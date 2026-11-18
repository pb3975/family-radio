# Public release scope

This edition starts with a fresh source snapshot. It contains the host software, Dial firmware, Wi-Fi service templates, synthetic fixtures, tests, verification tools, enclosure models and printable meshes. Private development history, household music, device logs, deployment records and screenshots are outside the release.

The public edition uses the Family Radio name for software and device identifiers. Service names, installation paths and device settings can depend on those identifiers. Build the host and Dial from the same revision. Existing installations require an explicit migration of any saved configuration or data they need. This release has no automatic migration from the prototype.

Some network code requires numeric addresses. The loopback interface lets demos run on the same computer as the browser. The temporary setup access point uses a predefined private subnet to supply addresses to the phone. These constants describe how the software works. No household network configuration is required to run the tests. Keep local media, credentials, login links, OAuth files and device logs outside the repository.
