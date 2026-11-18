#!/usr/bin/env python3
"""Validate synthetic setup artifacts using their native parsers, offline."""
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from host.wifi_network import AP_UUID, keyfile


def checked(arguments, **kwargs):
    result = subprocess.run(arguments, capture_output=True, text=True, **kwargs)
    if result.returncode:
        raise SystemExit(f'Artifact parser failed: {arguments[0]}\n{result.stderr}')
    return result.stdout


def main():
    for ap, ssid, password in ((True, 'Synthetic setup', 'synthetic-only'),
                                (False, ' Home\\;é ', ' space\\password ')):
        value = keyfile(AP_UUID, ssid, 'wpa2', password, ap=ap)
        normalized = checked(['nmcli', '--offline', 'connection', 'modify', 'connection.autoconnect', 'false'], input=value)
        second = checked(['nmcli', '--offline', 'connection', 'modify', 'connection.autoconnect', 'false'], input=normalized)
        if normalized != second or 'synthetic-only' not in normalized and ap:
            raise SystemExit('NetworkManager keyfile round trip changed the synthetic profile.')
    checked(['dnsmasq', '--test', '--conf-file=' + str(ROOT / 'deploy/wifi/dnsmasq.conf')])
    with tempfile.TemporaryDirectory() as directory:
        units = Path(directory)
        deploy = ROOT / 'deploy'
        for source in [*deploy.glob('*/*.service'), *deploy.glob('*/*.socket')]:
            (units / source.name).write_bytes(source.read_bytes())
        # The host need not run NM; a stub satisfies dependency resolution only.
        (units / 'NetworkManager.service').write_text('[Service]\nExecStart=/bin/true\n')
        checked(['systemd-analyze', 'verify', '--man=no', *map(str, [*units.glob('*.service'), *units.glob('*.socket')])])
    # nft's check mode still needs a network namespace with CAP_NET_ADMIN.
    checked(['unshare', '--net', 'nft', '--check', '--file', str(ROOT / 'deploy/wifi/isolation.nft')])
    print('NetworkManager keyfiles, systemd units, dnsmasq and nftables passed native offline validation.')


if __name__ == '__main__':
    main()
