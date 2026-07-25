from __future__ import annotations

import ipaddress
import socket
import webbrowser
from pathlib import Path

from .config import load_config
from .server_runtime import (
    run_server as run_local_server,
    validate_bind_host,
    validate_remote_write,
)


def local_access_url(host: str, port: int, server_url: str) -> str:
    if host in {"", "0.0.0.0"}:
        return f"http://127.0.0.1:{port}/"
    if host == "::":
        return f"http://[::1]:{port}/"
    return server_url


def run_server_command(
    target: Path,
    *,
    host: str,
    port: int,
    repo_root: Path,
    browser: bool = True,
    allow_remote: bool = False,
    allow_remote_write: bool = False,
    preview_images: bool = False,
    read_only: bool = False,
    lan_share: bool = False,
    slideshow_delay_seconds: int | None = None,
    slideshow_filter: str | None = None,
) -> int:
    validate_bind_host(host, allow_remote=allow_remote)
    validate_remote_write(
        host,
        read_only=read_only,
        allow_remote_write=allow_remote_write,
    )
    config = load_config(repo_root, migrate_legacy=not read_only)
    print("Starter Bildebank-server. Dette kan ta noen sekunder.")
    print(f"Bildesamling: {target}")

    def on_ready(url: str) -> None:
        access_url = local_access_url(host, port, url)
        print(f"Bildebank-serveren er klar: {access_url}")
        if lan_share:
            print_lan_share_warning(port)
        print("Trykk Ctrl-C for å stoppe serveren.")
        if browser:
            print("Åpner nettleser.")
            webbrowser.open(access_url)

    run_local_server(
        target,
        config,
        host=host,
        port=port,
        allow_remote=allow_remote,
        allow_remote_write=allow_remote_write,
        preview_images=preview_images,
        read_only=read_only,
        lan_share=lan_share,
        slideshow_delay_seconds=slideshow_delay_seconds,
        slideshow_filter=slideshow_filter,
        ready=on_ready,
    )
    return 0


def print_lan_share_warning(port: int) -> None:
    print("LAN-share er aktiv: read-only, preview-bilder og tilgang fra andre enheter på LAN.")
    print(
        "Lokale kilde- og snapshotbaner skjules. Originalbilder, nøyaktig GPS, "
        "kommentarer, personer og tagger er fortsatt tilgjengelige."
    )
    print(
        "ADVARSEL: Serveren kan nås av alle på samme LAN. "
        "Bildene kan dermed bli eksponert til alle på samme nettverk."
    )
    print("Ikke bruk --lan-share på offentlige nettverk, gjestenett eller nettverk du ikke stoler på.")
    urls = lan_share_urls(port)
    if not urls:
        print(f"Fant ikke lokal LAN-adresse automatisk. Finn IP-adressen med ipconfig og åpne http://<IP-adresse>:{port}/")
        return
    if len(urls) == 1:
        print(f"Åpne denne adressen på andre enheter: {urls[0]}")
        return
    print("Åpne en av disse adressene på andre enheter:")
    for url in urls:
        print(f"  {url}")


def lan_share_urls(port: int) -> list[str]:
    return [f"http://{address}:{port}/" for address in local_lan_ipv4_addresses()]


def local_lan_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    add_primary_lan_ipv4_address(addresses)
    add_hostname_lan_ipv4_addresses(addresses)
    return sorted(addresses, key=ipv4_sort_key)


def add_primary_lan_ipv4_address(addresses: set[str]) -> None:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 80))
            add_lan_ipv4_address(addresses, sock.getsockname()[0])
    except OSError:
        return


def add_hostname_lan_ipv4_addresses(addresses: set[str]) -> None:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        return
    for info in infos:
        add_lan_ipv4_address(addresses, str(info[4][0]))


def add_lan_ipv4_address(addresses: set[str], raw_address: str) -> None:
    try:
        address = ipaddress.ip_address(raw_address)
    except ValueError:
        return
    if address.version != 4 or address.is_loopback:
        return
    if address.is_private or address.is_link_local:
        addresses.add(str(address))


def ipv4_sort_key(raw_address: str) -> tuple[int, int, int, int]:
    first, second, third, fourth = raw_address.split(".")
    return int(first), int(second), int(third), int(fourth)
