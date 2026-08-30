"""The container image must ship a working BBS, not just the Python files.

The image this replaces copied `*.py` and nothing else, so the web admin had
no templates and no stylesheets to serve -- and on a headless server the web
admin is the entire interface. It also ran only server.py, left the web admin
bound to 127.0.0.1 where nothing outside the container could reach it, and
kept every piece of runtime state in the application directory, which an image
update replaces wholesale.

None of that shows up until someone actually runs the image, which is why
these assertions are here: they check the Dockerfile and entrypoint against
what the code genuinely loads and writes at runtime.
"""
import re
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DOCKERFILE = (REPO / "docker" / "Dockerfile").read_text(encoding="utf-8")
ENTRYPOINT = (REPO / "docker" / "entrypoint.sh").read_text(encoding="utf-8")
COMPOSE = (REPO / "docker" / "docker-compose.yaml").read_text(encoding="utf-8")
DOCKERIGNORE = (REPO / ".dockerignore").read_text(encoding="utf-8")
BUILD_SH = (REPO / "docker" / "build.sh").read_text(encoding="utf-8")
WORKFLOW = (REPO / ".github" / "workflows" / "docker-publish.yml").read_text(encoding="utf-8")
UNRAID_XML = REPO / "docker" / "baconbs-unraid.xml"

# Comments in these files quote the very strings being asserted on, so an
# assertion against the raw text can pass on a comment while the code below it
# says something else entirely.
ENTRYPOINT_CODE = "\n".join(
    line for line in ENTRYPOINT.splitlines() if not line.lstrip().startswith("#"))

CONTAINER_PORT = "8081"


def _copied_paths():
    """Sources named by COPY lines, ignoring the --chmod style flags."""
    paths = []
    for line in DOCKERFILE.splitlines():
        line = line.strip()
        if not line.startswith("COPY "):
            continue
        parts = [p for p in line.split()[1:] if not p.startswith("--")]
        paths.extend(parts[:-1])  # last token is the destination
    return paths


def _dockerfile_env():
    """ENV assignments, including the backslash-continued blocks."""
    env = {}
    collapsed = re.sub(r"\\\s*\n\s*", " ", DOCKERFILE)
    for line in collapsed.splitlines():
        line = line.strip()
        if not line.startswith("ENV "):
            continue
        for name, value in re.findall(r"([A-Za-z_][A-Za-z0-9_]*)=(\S+)", line[4:]):
            env[name] = value.strip('"')
    return env


class RuntimeAssetsAreInTheImageTests(unittest.TestCase):
    """What the code opens at runtime has to be in the image."""

    def test_the_web_admin_templates_are_copied(self):
        """Flask is constructed with template_folder='templates'. Without it
        every page is a TemplateNotFound, which is what the old image did."""
        self.assertIn("templates/", _copied_paths())

    def test_the_web_admin_static_files_are_copied(self):
        self.assertIn("static/", _copied_paths())

    def test_the_python_modules_are_copied(self):
        self.assertIn("*.py", _copied_paths())

    def test_data_files_opened_by_name_are_copied(self):
        """command_handlers.py opens fortunes.txt relative to the working
        directory, and the entrypoint seeds config from example_config.ini."""
        copied = _copied_paths()
        self.assertIn("fortunes.txt", copied)
        self.assertIn("example_config.ini", copied)

    def test_the_working_directory_is_where_those_files_land(self):
        """fortunes.txt is opened by bare name, so a mismatched WORKDIR turns
        the fortune command into a FileNotFoundError."""
        workdirs = re.findall(r"^WORKDIR\s+(\S+)", DOCKERFILE, re.M)
        self.assertTrue(workdirs)
        self.assertIn(f"cd \"$APP_DIR\"", ENTRYPOINT)
        self.assertIn(f"APP_DIR={workdirs[-1]}", ENTRYPOINT)


class BothProcessesRunTests(unittest.TestCase):
    """server.py drives the radios; web_admin.py serves the GUI. They are
    separate processes that signal each other through files, and the old image
    started only the first one."""

    def test_the_bbs_is_started(self):
        self.assertRegex(ENTRYPOINT_CODE, r"run\s+server\.py")

    def test_the_web_admin_is_started(self):
        self.assertRegex(ENTRYPOINT_CODE, r"run\s+web_admin\.py")

    def test_both_run_in_the_background_so_neither_blocks_the_other(self):
        for process in ("server.py", "web_admin.py"):
            self.assertRegex(ENTRYPOINT_CODE,
                             rf"run\s+{re.escape(process)}[^\n]*&")

    def test_the_supervisor_watches_for_a_child_exiting(self):
        self.assertRegex(ENTRYPOINT_CODE, r"(?m)^\s*wait -n\s*$")

    def test_signals_reach_the_children(self):
        self.assertRegex(ENTRYPOINT_CODE, r"trap\s+\w+\s+TERM")


class ControlPlaneSurvivesTests(unittest.TestCase):
    """The web admin is the only way to configure this node, so a BBS that
    cannot start must not take it down with it.

    Tearing the container down on any server.py exit was tried first, and it
    locks the operator out: the container restarts, the web admin lives for
    the fraction of a second before the BBS fails again, and the GUI is
    unreachable in practice -- with the fix for the problem only available
    through that GUI.
    """

    def test_the_bbs_is_restarted_rather_than_ending_the_container(self):
        self.assertRegex(ENTRYPOINT_CODE, r"start_bbs\b[\s\S]*start_bbs\b")
        self.assertIn("restarting in", ENTRYPOINT_CODE)

    def test_the_restart_backs_off(self):
        """A BBS failing instantly would otherwise spin the log."""
        self.assertRegex(ENTRYPOINT_CODE, r"delay=\$\(\(\s*delay \* 2\s*\)\)")

    def test_the_container_still_exits_if_the_web_admin_dies(self):
        """Nothing left to manage, so let the restart policy take it."""
        self.assertRegex(
            ENTRYPOINT_CODE,
            r"kill -0 \"\$WEB_PID\"[\s\S]{0,400}?exit 1")

    def test_a_stop_during_a_backoff_is_not_delayed(self):
        """bash runs a trap between commands, never during one, so a single
        long sleep would stall docker stop until it was SIGKILLed."""
        self.assertIn("interruptible_sleep", ENTRYPOINT_CODE)
        self.assertRegex(ENTRYPOINT_CODE, r"(?m)^\s*sleep 1\s*$")


class HealthReflectsBothHalvesTests(unittest.TestCase):
    """With the web admin kept alive through a BBS crash, checking only the
    GUI would report a node that moves no mail at all as healthy."""

    def setUp(self):
        self.healthcheck = (REPO / "docker" / "healthcheck.py").read_text(encoding="utf-8")

    def test_the_image_uses_the_healthcheck_script(self):
        self.assertIn("baconbs-healthcheck", DOCKERFILE)
        self.assertIn("COPY docker/healthcheck.py", DOCKERFILE)

    def test_it_checks_the_web_admin(self):
        self.assertIn("/login", self.healthcheck)

    def test_it_checks_that_the_bbs_is_running(self):
        self.assertIn("server.py", self.healthcheck)
        self.assertIn("/proc", self.healthcheck)

    def test_it_needs_no_packages_the_image_does_not_have(self):
        """Reading /proc rather than shelling out to pgrep keeps procps out
        of the image."""
        self.assertNotIn("pgrep", self.healthcheck)
        self.assertNotIn("subprocess", self.healthcheck)


class NetworkReachabilityTests(unittest.TestCase):
    def test_the_web_admin_binds_all_interfaces(self):
        """web_admin.py defaults to 127.0.0.1, which inside a network
        namespace means no published port can ever reach it."""
        self.assertEqual(_dockerfile_env().get("BBS_WEBGUI_HOST"), "0.0.0.0")

    def test_the_port_is_exposed_and_published(self):
        self.assertIn(f"EXPOSE {CONTAINER_PORT}", DOCKERFILE)
        self.assertIn(f"{CONTAINER_PORT}:{CONTAINER_PORT}", COMPOSE)

    def test_the_healthcheck_uses_a_route_that_answers_without_a_session(self):
        """Hitting an authenticated route would report unhealthy forever."""
        self.assertIn("HEALTHCHECK", DOCKERFILE)
        self.assertIn("/login", DOCKERFILE)


class StateSurvivesAnUpdateTests(unittest.TestCase):
    """Every runtime path defaults to the application directory, which an
    image update replaces. Each one has to be redirected to the volume."""

    @staticmethod
    def _runtime_path_env_vars():
        names = set()
        for source in ("server.py", "web_admin.py"):
            text = (REPO / source).read_text(encoding="utf-8")
            names.update(re.findall(
                r"resolve_app_path\(\s*os\.getenv\(\s*[\"']([A-Z0-9_]+)[\"']", text))
        return names

    def test_every_runtime_path_points_at_the_volume(self):
        env = _dockerfile_env()
        volume = "/config"
        misplaced = {}
        for name in sorted(self._runtime_path_env_vars()):
            value = env.get(name)
            if value is None or not value.startswith(volume):
                misplaced[name] = value
        self.assertEqual(
            misplaced, {},
            "these would be written into the image layer and lost on update: %r"
            % (misplaced,))

    def test_the_trigger_files_the_gui_writes_are_covered(self):
        """The web admin signals the BBS by touching these. Split across a
        replaced directory, 'Sync now' silently does nothing."""
        found = self._runtime_path_env_vars()
        for name in ("BBS_MANUAL_SYNC_TRIGGER_PATH",
                     "BBS_LINKS_RELOAD_TRIGGER_PATH",
                     "BBS_RUNTIME_DIAG_PATH"):
            self.assertIn(name, found)

    def test_zork_downloads_land_on_the_volume(self):
        """zork_port.py resolves story files as data/<name>, relative to the
        working directory, with no env var to point elsewhere."""
        self.assertRegex(DOCKERFILE, r"ln -s /config/data\s+/app/data")

    def test_the_question_set_ships_and_is_seeded(self):
        """data/ is excluded from the build context and /app/data is a
        symlink to the volume, so the questions need an explicit route in
        and an explicit copy out -- the same shape as config.ini."""
        self.assertIn("!data/trivia.db", DOCKERIGNORE)
        self.assertIn("COPY data/trivia.db", DOCKERFILE)
        self.assertIn("trivia-seed.db", ENTRYPOINT_CODE)

    def test_seeding_never_overwrites_a_local_question_set(self):
        """An operator who topped the set up with fetch_trivia_questions.py
        must not lose it to the smaller one baked into the image."""
        self.assertRegex(
            ENTRYPOINT_CODE,
            r"\[ ! -f \"\$CONFIG_DIR/data/trivia\.db\" \]")

    def test_the_volume_is_declared(self):
        self.assertIn("VOLUME /config", DOCKERFILE)
        self.assertIn(":/config", COMPOSE)


class VersionIsStampedTests(unittest.TestCase):
    """The image has no .git and no git binary, so version_info cannot resolve
    the commit count or hash the way it does on a normal install. Unstamped,
    every image ever built reports the same fallback version -- the failure
    this project already hit once."""

    def test_the_build_args_are_declared(self):
        self.assertIn("ARG BBS_BUILD_NUMBER", DOCKERFILE)
        self.assertIn("ARG BBS_GIT_COMMIT", DOCKERFILE)

    def test_the_build_args_become_environment_variables(self):
        env = _dockerfile_env()
        self.assertEqual(env.get("BBS_BUILD_NUMBER"), "${BBS_BUILD_NUMBER}")
        self.assertEqual(env.get("BBS_GIT_COMMIT"), "${BBS_GIT_COMMIT}")

    def test_the_names_match_what_version_info_reads(self):
        version_info = (REPO / "version_info.py").read_text(encoding="utf-8")
        self.assertIn("BBS_BUILD_NUMBER", version_info)
        self.assertIn("BBS_GIT_COMMIT", version_info)

    def test_the_build_helper_supplies_real_values(self):
        self.assertIn("rev-list --count HEAD", BUILD_SH)
        self.assertIn("rev-parse --short HEAD", BUILD_SH)

    def test_the_workflow_supplies_real_values(self):
        self.assertIn("rev-list --count HEAD", WORKFLOW)
        self.assertIn("BBS_BUILD_NUMBER=", WORKFLOW)

    def test_the_workflow_clones_deeply_enough_to_count(self):
        """A shallow clone counts its own truncated history, so the published
        version would be wrong rather than merely missing."""
        self.assertIn("fetch-depth: 0", WORKFLOW)

    def test_an_unstamped_build_says_so(self):
        self.assertIn("fallback version", ENTRYPOINT)


class BuildContextTests(unittest.TestCase):
    def test_secrets_and_per_node_state_stay_out_of_the_image(self):
        """An image is shared. One node's database, certificates or config
        travelling to whoever pulls it is a leak, not a convenience."""
        for pattern in ("data/", "*.db", "config.ini", "*.trigger",
                        "runtime_diagnostics.json", ".git"):
            self.assertIn(pattern, DOCKERIGNORE, f"{pattern} is in the build context")

    def test_the_stale_build_cache_is_excluded(self):
        """_build_version.py records the count of whatever checkout built the
        image, which is not necessarily what was stamped in."""
        self.assertIn("_build_version.py", DOCKERIGNORE)


class LineEndingTests(unittest.TestCase):
    """A CRLF shell script is unrunnable in the container.

    The kernel reads the shebang as "/bin/bash\\r" and the container dies with
    "no such file or directory" -- naming bash rather than the carriage
    return, which sends people looking for a missing interpreter that is
    plainly installed. This repository is edited on Windows, so git has to be
    told to keep these files LF on checkout.
    """

    # docker build copies the working tree, not the index, so for these the
    # bytes on disk are the bytes that reach the image. Every other shell
    # script in the repo is covered by the .gitattributes rule alone, since
    # those only ever run from a Linux checkout.
    BUILD_CONTEXT_SCRIPTS = ("docker/entrypoint.sh", "docker/build.sh")

    def test_git_checks_shell_scripts_out_with_lf(self):
        attributes = (REPO / ".gitattributes").read_text(encoding="utf-8")
        self.assertRegex(attributes, r"\*\.sh\s+text\s+eol=lf")

    def test_the_scripts_in_the_build_context_have_no_carriage_returns(self):
        offenders = [name for name in self.BUILD_CONTEXT_SCRIPTS
                     if b"\r" in (REPO / name).read_bytes()]
        self.assertEqual(offenders, [])

    def test_the_dockerfile_has_no_carriage_returns(self):
        """Its RUN lines are fed to /bin/sh, with the same result."""
        self.assertNotIn(b"\r", (REPO / "docker" / "Dockerfile").read_bytes())

    def test_the_entrypoint_is_executable_in_the_image(self):
        """Git on Windows does not carry the executable bit reliably, so the
        image sets it rather than trusting the checkout."""
        self.assertRegex(DOCKERFILE, r"chmod\s+755\s+/usr/local/bin/baconbs-entrypoint")


class UnraidTemplateTests(unittest.TestCase):
    def setUp(self):
        self.root = ET.parse(UNRAID_XML).getroot()
        self.configs = self.root.findall("Config")

    def test_it_is_a_container_template(self):
        self.assertEqual(self.root.tag, "Container")

    def test_the_web_ui_link_uses_the_published_port(self):
        webui = self.root.findtext("WebUI") or ""
        self.assertIn(f"[PORT:{CONTAINER_PORT}]", webui)

    def test_the_config_volume_is_offered(self):
        paths = [c for c in self.configs if c.get("Type") == "Path"]
        self.assertTrue(any(c.get("Target") == "/config" for c in paths),
                        "no /config mapping, so nothing would survive an update")

    def test_unraid_default_ownership_is_the_default(self):
        """Unraid runs containers as 99:100; files written as anything else
        show up unreadable in the share."""
        by_target = {c.get("Target"): (c.text or "").strip() for c in self.configs}
        self.assertEqual(by_target.get("PUID"), "99")
        self.assertEqual(by_target.get("PGID"), "100")

    def test_the_ownership_variables_are_honoured(self):
        self.assertIn("PUID", ENTRYPOINT_CODE)
        self.assertIn("PGID", ENTRYPOINT_CODE)

    def test_files_created_at_startup_are_reowned_too(self):
        """config.ini and the session secret are written during startup. A
        chown that runs before them leaves both owned by root, and a config
        the BBS cannot write means every save from Settings fails."""
        chown_at = ENTRYPOINT_CODE.index("chown -R")
        for created in ("example_config.ini", "secrets.token_hex"):
            self.assertLess(ENTRYPOINT_CODE.index(created), chown_at,
                            f"{created} is created after the chown")

    def test_a_usb_radio_can_be_attached(self):
        self.assertTrue(any(c.get("Type") == "Device" for c in self.configs))

    def test_the_radio_is_optional(self):
        """A node can run over TCP or MQTT with no radio at all, and marking
        the device required would block that install."""
        devices = [c for c in self.configs if c.get("Type") == "Device"]
        for device in devices:
            self.assertEqual(device.get("Required"), "false")

    def test_the_image_it_pulls_is_the_one_the_workflow_pushes(self):
        """Unraid installs from a registry; it does not build from source."""
        repository = (self.root.findtext("Repository") or "").strip()
        self.assertTrue(repository.startswith("ghcr.io/"), repository)
        image = repository.split(":")[0].split("/")[-1]
        self.assertIn(image, WORKFLOW)


if __name__ == "__main__":
    unittest.main()
