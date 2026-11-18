# Device interface

The host and Dial exchange B8 records over USB serial. Each record starts with the version, separates fields with tabs and ends with a newline. The maximum length before the newline is 1,152 bytes. Both endpoints must support the same version.

The Dial announces readiness, sends numbered control events and requests artwork. The host returns player snapshots and chunks of artwork. Session and view identifiers tie a command to the state the listener saw. Each player snapshot also carries a local calendar minute for the idle clock. Zero means that the clock is unavailable. Waking the screen consumes the first input so it does not change playback.

Wi-Fi setup uses its own request and response records within B8. A request identifies the session, sequence, request and operation. Its response identifies the setup session and revision, reports the state, and can supply temporary setup credentials for the Dial. Keep captured setup traffic private. The phone portal uses a separate version 3 interface.

Use the [B8 examples](fixtures/protocol-b8.json) and [Wi-Fi setup examples](fixtures/wifi-setup-v3.json) when testing an integration. Older fixtures describe earlier revisions. The host and firmware source define the validation rules for the current version.
