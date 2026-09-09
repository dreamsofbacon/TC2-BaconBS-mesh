# Bacon BBS overhaul

The consolidated PRD is published as [GitHub issue #2](https://github.com/materva/TC2-BaconBS-mesh/issues/2), labeled `ready-for-agent`. A local copy is available in [the PRD document](PRD-NETWORK-PARITY-OVERHAUL.md).

## Agreed direction

- Give Meshtastic and MeshCore equal everyday capability, using each network's terminology and actual channel names.
- Provide one Radios page with separate panels for the connected radios. Include connection status, contacts, channels, and supported settings.
- Allow everyday radio administration within Bacon BBS while the BBS remains running.
- Add message sending to public chatter, initially for administrators. Require an explicit radio and named channel selection before sending.
- Initially send chatter through radios attached to the current BBS. Sending through remote BBS nodes is a future requirement; its permissions and offline behavior remain undecided. The existing web chatter page has no sending facility to preserve.
- The first radio-management release covers status, contacts, named channels, messaging, and basic identity settings. Advanced frequency, power, routing, and firmware management are deferred until the foundation is reliable on both networks.
- Preserve normal upgrades for existing installations: retain accounts, messages, and configuration; preserve offline operation and communication with older peers during staggered upgrades. Supply migrations where needed rather than requiring a fresh installation. Internal code and interface organization may change substantially within these constraints.
- Support Meshtastic only, MeshCore only, and both together as required configurations. Specific devices are not a planning dependency; radios will be connected when hardware validation is needed.
- Audit bugs systematically and verify fixes. Zero remaining bugs is not a provable acceptance criterion.

## Agreed sequence

1. Audit and fix startup, data, synchronization, and radio-connection problems.
2. Build the shared Radios page, improve network terminology, and add local chatter sending.
3. Improve navigation, setup, documentation, and code organization.

Each stage must leave a working, upgradeable project, with relevant fixes and verification completed along the way.

## Validation timing

Begin with automated checks and simulation. Hardware validation follows when radios are connected; do not treat hardware availability as a prerequisite for implementation or claim simulated checks prove radio operation.

## Findings from initial code inspection

- The device page supports two radio cards, but navigation still calls it Meshtastic and MeshCore gets administration instructions rather than controls.
- The web chatter page provides history, search, and filters; it has no message composer.
- Chatter already attempts to resolve channel names, with numeric fallbacks. The reported naming problem needs tracing through capture, storage, and display.

These findings are an initial inventory, not a completed bug audit.
