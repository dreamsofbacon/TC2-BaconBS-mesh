# Bacon BBS reliability and Meshtastic–MeshCore network parity overhaul

## Problem Statement

Bacon BBS supports Meshtastic and MeshCore, but its everyday experience favors Meshtastic. Navigation names Meshtastic, an optional Meshtastic web client is available, and MeshCore users are directed to an external client for radio administration. Public chatter can expose channel numbers or inappropriate defaults instead of the names operators recognize, such as MeshCore's #Public.

Operators want a reliable, coherent BBS whether they connect one Meshtastic radio, one MeshCore radio, or both. They also want an extensive bug audit and improvements to setup, navigation, documentation, and maintainability. Existing accounts, messages, configuration, offline use, and communication with peers must survive the overhaul.

Initial code inspection identifies uneven product behavior, not a completed bug audit. Finding and fixing every bug is the ambition; claiming that no bugs remain is not a verifiable completion criterion.

## Solution

Deliver network parity: equal everyday capability using each network's terminology and supported capabilities. Provide one Radios page with a separate panel for each connected radio, including status, contacts, named channels, messaging, and basic identity settings. Everyday administration should operate through Bacon BBS while the BBS remains running.

Add administrator-only sending to public chatter through an explicitly selected local radio and channel. Continue displaying chatter heard by participating nodes while clearly distinguishing its source from available transmission destinations. Plan for remote sending later without implementing it in this release.

Deliver in three working, upgradeable stages:

1. Establish a baseline, systematically audit the application, and fix startup, data, synchronization, and radio-connection problems first.
2. Deliver the shared Radios page, correct network terminology, and local chatter sending.
3. Improve navigation, setup, documentation, and code organization across the project.

## User Stories

1. As a Meshtastic operator, I want Bacon BBS to work with only my Meshtastic radio, so that MeshCore hardware is not required.
2. As a MeshCore operator, I want Bacon BBS to work with only my MeshCore radio, so that Meshtastic hardware is not required.
3. As a dual-network operator, I want both radios to work together, so that one BBS can serve both networks.
4. As an operator, I want either network to be usable as the primary connection, so that setup does not privilege Meshtastic.
5. As an existing operator, I want my saved configuration to remain usable after upgrading, so that I do not have to rebuild my installation.
6. As a BBS user, I want my account, linked identity, mail, and other saved content retained, so that an upgrade does not erase my history.
7. As an operator, I want continued communication with older peers during staggered upgrades, so that all nodes need not update simultaneously.
8. As a field operator, I want everyday features to work without internet access, so that the BBS remains useful off-grid.
9. As an operator, I want startup failures to identify the affected connection and cause, so that I can resolve them.
10. As a dual-network operator, I want one radio's outage to leave the other usable, so that a single failure does not stop the BBS.
11. As an operator, I want radio recovery to avoid lost state and duplicate actions, so that reconnecting is dependable.
12. As an operator, I want connection status to distinguish configured, connected, reconnecting, and unavailable radios, so that I know what is actually usable.
13. As an operator, I want one Radios page showing each radio and its network, so that I can understand my installation at a glance.
14. As a MeshCore operator, I want routine radio controls within Bacon BBS, so that basic administration does not require another client.
15. As a Meshtastic operator, I want the same everyday administration workflow, so that the interface is consistent across networks.
16. As an operator, I want to inspect contacts or known nodes using the appropriate network vocabulary, so that I can recognize reachable participants.
17. As an operator, I want supported contact management to be clearly available, so that I can maintain the radio's contact information.
18. As an operator, I want to inspect configured channels by name, so that I do not have to memorize numeric positions.
19. As an operator, I want basic radio identity settings to be editable where supported, so that other participants can recognize my node.
20. As an operator, I want unsupported operations clearly identified, so that I do not mistake a network limitation for a broken control.
21. As an operator, I want routine administration while the BBS keeps running, so that I do not have to hand the radio connection to another application.
22. As an administrator, I want setting changes to report success or failure accurately, so that I know whether the radio accepted them.
23. As a chatter reader, I want familiar channel names such as #Public when appropriate, so that conversations are easy to identify.
24. As a chatter reader, I want missing channel names to be shown honestly, so that a guessed name does not misidentify a conversation.
25. As a chatter reader, I want the network and receiving BBS node visible, so that I understand where a message was heard.
26. As a chatter reader, I want unrelated channels with the same numeric position kept distinct, so that filters do not merge different conversations.
27. As a chatter reader, I want existing search, history, and filtering preserved, so that the overhaul does not remove useful tools.
28. As an administrator, I want to compose a channel message in chatter, so that I can participate as well as monitor.
29. As an administrator, I want to explicitly select the local radio and named channel before sending, so that messages go to the intended destination.
30. As an administrator, I want remote observations distinguished from locally available send targets, so that selecting a remote conversation cannot silently transmit somewhere else.
31. As an administrator, I want an unavailable destination to fail clearly, so that a message is not rerouted to another radio or channel.
32. As an administrator, I want message limits checked for the selected network, so that content is not silently truncated.
33. As an administrator, I want send status to distinguish submission from confirmed delivery where confirmation exists, so that the interface does not overstate success.
34. As an operator, I want chatter transmission restricted to authenticated administrators, so that the existing reading experience does not grant transmission privileges.
35. As an operator, I want transmission to respect existing radio pacing, so that interactive use does not bypass bandwidth protections.
36. As an existing operator, I want MQTT-only installations to remain supported, so that the radio overhaul does not invent a radio requirement for them.
37. As an operator, I want clear setup guidance for Meshtastic only, MeshCore only, and both, so that I can configure the installation I actually have.
38. As an operator, I want consistent navigation and terminology across settings, chatter, radio pages, and help, so that I do not need to translate between conflicting labels.
39. As an operator, I want usable empty, loading, disconnected, and error states, so that missing data is understandable.
40. As a user, I want existing mail, bulletins, profiles, games, and other BBS functions protected by the audit, so that radio improvements do not regress unrelated features.
41. As a maintainer, I want reproducible bug reports and regression checks, so that verified fixes stay fixed.
42. As a maintainer, I want isolated components with stable behavior contracts, so that supporting either network does not spread special cases throughout the application.
43. As a maintainer, I want automated validation before hardware is available, so that implementation can proceed immediately.
44. As an operator, I want real-radio validation for all three required configurations before radio readiness is claimed, so that simulated success is not mistaken for field reliability.

## Implementation Decisions

The product scope and delivery order above are agreed. The following component boundaries are recommended implementation structure, subject to review during implementation; they do not mandate a rewrite or a new framework.

1. **Radio connections and recovery.** Encapsulate connection creation, lifecycle, network identity, capability discovery, channel/contact snapshots, and independent recovery behind a small per-radio interface. Preserve existing supported transports and keep primary/secondary placement separate from network type. Absence of one radio must not imply failure of the other. An MQTT-only installation must not display a fictitious Meshtastic device.
2. **Radio controls.** Provide a narrow service for reading status and performing supported contact, channel-selection, and basic identity operations. The running BBS owns radio access; web requests must reach that owner rather than competing for the serial, TCP, or BLE connection. Select the local command exchange mechanism during implementation. Serialize conflicting radio operations, bound waits, and return explicit outcomes. Inspect named channels and select them for messaging in the initial scope; advanced channel encryption provisioning and broad radio reconfiguration are not implied.
3. **Chatter history and sending.** Keep history capture and display distinct from transmission. Represent a local send destination using radio identity and its channel identity; a display name alone is insufficient. Never treat equal channel indexes across networks or receiving nodes as proof of a shared conversation. Do not infer a new cross-node channel identity scheme solely from channel names. Preserve existing stored identities and reconcile naming improvements compatibly.
4. **Data upgrades and synchronization.** Encapsulate compatibility-sensitive persistence and synchronization behavior behind operations that can be tested against old data and peers. Preserve accounts, content, configuration, tombstones, and existing retention semantics. Add only necessary, repeatable migrations. Existing bridge behavior synchronizes shared BBS records; adding a chatter composer must not introduce automatic radio-to-radio chatter forwarding. Preserve existing MQTT chatter distribution.
5. **Web interface and setup.** Present one Radios destination with per-radio panels and capability-aware controls. Integrate an administrator-only chatter composer and network-correct labels across web and radio-facing help. Reuse established authentication and request protection. Preserve old entry points through compatibility routing where needed. Improve responsive layout, keyboard access, and meaningful status states without requiring an external hosted client for core workflows.
6. **Sending contract.** A request identifies an explicitly chosen local radio, channel, and text. The service validates authority, current destination availability, and network-specific byte limits before dispatch. An unavailable radio never falls back to another. Prevent duplicate dispatch caused by repeat submission of the same operation. Report queued/submitted, failure, and confirmation only to the extent supported by the radio. Do not automatically retry an uncertain send in a way that duplicates public messages.
7. **Naming contract.** Prefer actual radio channel names. Format network conventions appropriately without adding duplicate prefixes. Distinguish unknown names from established names; do not assume channel zero always means #Public or LongFast. Trace the reported naming issue through capture, persistence, filters, and display before changing only the visible text.
8. **Compatibility.** Preserve currently supported deployment environments and dependency markers, including older non-MeshCore installations, unless a separate future decision changes that support. New web features must not force older peers to understand new protocol messages. No replacement database or wire-protocol redesign is approved by this PRD.
9. **Audit method.** Build a reproducible baseline and a findings ledger recording observed behavior, expected behavior, impact, evidence, fix, and verification. Review startup, persistence, synchronization, radio recovery, authentication, input handling, configuration, and existing BBS features. Prioritize data loss and service failures before cosmetic changes. Refactor around demonstrated needs rather than changing all large modules at once.

## Testing Decisions

- Test observable behavior at component boundaries, not internal call sequences or helpers that merely repeat the implementation. A useful regression test fails for the identified defect and passes after the fix.
- Recommended coverage includes all five components above. Radio adapters and controls need contract checks; chatter needs capture, naming, authorization, destination, duplicate-submission, byte-limit, and outcome checks; persistence needs migration and synchronization checks; the web interface needs meaningful integrated user-flow checks. Cosmetic-only changes do not require mirrored tests.
- Use isolated temporary databases and configuration, deterministic fake radios, and recorded protocol-shaped events. Do not alter operational databases or connect to live peers while running ordinary automated tests.
- Exercise Meshtastic only, MeshCore only, both networks with either primary placement, disconnected radios, and preserved MQTT-only operation. In dual-radio checks, one failing or stalled connection must leave the other functional.
- Verify channel zero with a custom name, missing names, late-arriving names, Unicode names and messages, identical indexes on distinct radios, remote chatter observations, reconnect during sending, and unsupported commands. Unauthorized or invalid requests must cause no transmission.
- Check migrations using representative pre-upgrade data and configuration, repeated execution, preserved records, and continued exchange with older peer capabilities. Exercise supported Python/SQLite differences where they affect behavior; success on one development environment does not establish that matrix.
- Prior art includes existing fake-radio adapter tests, dual-interface bridge and configuration tests, independent recovery and watchdog tests, transport packet-size tests, public chatter capture/web/filter/hops/sender tests, authenticated web tests, temporary-database account tests, peer-capability tests, and synchronization timestamp/hash tests. Reuse their useful fixtures while correcting any unrealistic protocol assumptions.
- Run the existing suite to establish the baseline, targeted regression tests for fixes, and relevant broader checks at each stage. Record pre-existing failures separately rather than claiming them as regressions or silently ignoring them.
- Hardware is not a planning or implementation prerequisite. When devices are connected, verify receive/send, real channel names, supported controls, reconnect, and concurrent BBS operation in all three required configurations. Record tested firmware, library, and transport combinations and clearly mark untested combinations.

## Out of Scope

- Sending messages through remote BBS nodes in this release. This remains a future requirement with authority, routing, and offline behavior to design later.
- Automatic bridging of public chatter transmissions between networks.
- Advanced frequency, transmit power, routing, firmware flashing, and comprehensive radio configuration in the first radio-management release.
- A complete replacement for every feature in external Meshtastic or MeshCore clients, or forced feature symmetry where the networks differ.
- Granting ordinary BBS users new web transmission privileges.
- Requiring particular radio models or delaying implementation until devices are selected.
- A fresh-install-only migration, deliberate loss of existing data, or coordinated simultaneous upgrades of all peers.
- A guarantee that every possible bug has been eliminated.
- Deploying to running installations or transmitting real test messages as part of writing this PRD.

## Further Notes

Completion is assessed by stage, with evidence:

- **Reliability stage:** establish the test baseline; inventory and prioritize reproducible findings; verify fixes for identified critical startup, data, sync, and connection failures; explicitly document unresolved issues and environmental limitations.
- **Network parity stage:** all three required configurations support the agreed everyday flows; both networks appear appropriately in navigation and chatter; authenticated administrators can send only to explicitly chosen local targets; unsupported and disconnected states are honest; upgrade compatibility is verified.
- **Broader improvement stage:** setup and documentation cover all three configurations, navigation is consistent, existing BBS workflows retain their behavior, and component boundaries make future maintenance easier without an unnecessary wholesale rewrite.

Automated readiness and hardware readiness must be reported separately. Devices will be connected when hardware testing is needed. No specific hardware inventory is required now.

The operator approved the product scope through the planning conversation. Recommended component boundaries and test coverage are included to make implementation actionable without extending the interview. Exact endpoint names, command exchange technology, and schema additions remain implementation choices constrained by this PRD.
