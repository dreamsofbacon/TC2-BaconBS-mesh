"""Bulletins, Mail and Channels in the web admin, as lists of posts.

Reported 2026-09-29: a bulletin posted over Twitch showed no name anywhere
in the web admin. The page was a grid of raw database columns: the author
sat under "Sender Short Name", held the name as typed at posting rather than
the account's, and on a narrow screen the card layout cut the label to "Se"
and dropped the value entirely.

Each post now leads with its subject and its author, resolved the way the
radio menus resolve it, and the page reads the same at any width.
"""
import ssh_auth
import db_operations
from web_admin import create_app
from test_web_admin import _WebAdminHarness


class PostListTests(_WebAdminHarness):
    def setUp(self):
        super().setUp()
        db_operations.initialize_database()
        self.client = create_app().test_client()
        self.login(self.client)

    def page(self, path):
        return self.client.get(path).get_data(as_text=True)

    def ssh_account(self, alias):
        password_hash, salt = ssh_auth.hash_password("pw-pw-pw-pw-pw")
        return "ssh:" + db_operations.create_ssh_account(alias, password_hash, salt)

    def set_alias(self, node_id, alias):
        conn = db_operations.get_db_connection()
        conn.execute(
            "UPDATE accounts SET alias = ? WHERE account_id = "
            "(SELECT account_id FROM linked_nodes WHERE node_id = ?)", (alias, node_id))
        conn.commit()

    # --- the report -----------------------------------------------------

    def test_a_twitch_post_shows_its_author(self):
        author = self.ssh_account("twitch-48151623")
        db_operations.add_bulletin("General", "twitch-48151623", "Hello from Twitch",
                                   "Posted over a whisper", [], None, author_node_id=author)
        page = self.page("/bulletins")
        self.assertIn('<span class="post-author">twitch-48151623</span>', page)
        self.assertIn("Hello from Twitch", page)

    def test_the_author_follows_the_account_as_the_radio_does(self):
        """The stored name is what was typed at posting; the account's
        alias is who they are now, and it is what the radio shows."""
        author = self.ssh_account("oldname")
        db_operations.add_bulletin("General", "oldname", "Renamed", "Body", [], None,
                                   author_node_id=author)
        self.set_alias(author, "newname")
        page = self.page("/bulletins")
        self.assertIn('<span class="post-author">newname</span>', page)
        self.assertNotIn('<span class="post-author">oldname</span>', page)

    def test_a_post_with_no_account_keeps_its_stored_name(self):
        db_operations.add_bulletin("Urgent", "NNE1", "Road closed", "Use the bypass",
                                   [], None, author_node_id="!a1b2c3d4")
        self.assertIn('<span class="post-author">NNE1</span>', self.page("/bulletins"))

    def test_searching_finds_a_post_by_its_author(self):
        author = self.ssh_account("twitch-1234")
        db_operations.add_bulletin("General", "someone", "Findable", "Body", [], None,
                                   author_node_id=author)
        db_operations.add_bulletin("General", "other", "Not this one", "Body", [], None)
        page = self.page("/bulletins?q=twitch-1234")
        self.assertIn("Findable", page)
        self.assertNotIn("Not this one", page)

    def test_no_database_column_names_are_shown(self):
        db_operations.add_bulletin("General", "NNE1", "Subject", "Body", [], None)
        page = self.page("/bulletins")
        self.assertNotIn("Sender Short Name", page)
        self.assertNotIn("sender_short_name", page)

    # --- sync state -----------------------------------------------------

    def test_resolve_is_offered_only_where_there_is_something_to_resolve(self):
        db_operations.add_bulletin("General", "A", "Whole post", "All here", [], None)
        db_operations.add_bulletin("General", "B", "Half a post", "Only part", [], None)
        conn = db_operations.get_db_connection()
        conn.execute("UPDATE bulletins SET content_complete = 0, "
                     "expected_content_length = 99 WHERE subject = 'Half a post'")
        conn.commit()
        page = self.page("/bulletins")
        self.assertEqual(1, page.count(">Resolve</button>"))
        self.assertIn("Incomplete", page)

    def test_a_post_with_no_recorded_origin_does_not_say_unknown(self):
        conn = db_operations.get_db_connection()
        db_operations.add_bulletin("General", "A", "Old post", "Body", [], None)
        conn.execute("UPDATE bulletins SET source_node_id = NULL")
        conn.commit()
        self.assertNotIn("from unknown", self.page("/bulletins"))

    # --- mail and channels ----------------------------------------------

    def test_mail_says_who_it_is_from_and_to_by_account(self):
        viewer = self.ssh_account("twitch-555")
        host = self.ssh_account("bacon")
        db_operations.add_mail(viewer, "twitch-555", host, "Loving the BBS", "Hi", [], None)
        page = self.page("/mail")
        self.assertIn('<span class="post-author">twitch-555</span>', page)
        self.assertIn('<span class="post-author">bacon</span>', page)
        self.assertNotIn(host, page)  # the name, never the raw ssh: id

    def test_channels_show_their_description_and_comment_count(self):
        db_operations.add_channel("Stream nights", "Tuesdays on Twitch", [], None)
        channel_id = db_operations.get_db_connection().execute(
            "SELECT id FROM channels WHERE name = 'Stream nights'").fetchone()[0]
        db_operations.add_channel_comment(channel_id, "viewer", "See you there", [], None)
        page = self.page("/channels")
        self.assertIn("Stream nights", page)
        self.assertIn("Tuesdays on Twitch", page)
        self.assertIn("1 comment<", page)

    def test_an_empty_page_says_so(self):
        self.assertIn("No channels found", self.page("/channels"))
