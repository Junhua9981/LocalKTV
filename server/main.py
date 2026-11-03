"""Entry point: `python -m ktv` or `python server/main.py`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn  # noqa: E402

from ktv import config, logsetup  # noqa: E402
from ktv.app import create_app  # noqa: E402
from ktv.pipeline import Pipeline  # noqa: E402
from ktv.rooms import Rooms  # noqa: E402
from ktv.store import Store, TrackCache  # noqa: E402


def print_banner(join_url: str) -> None:
    print()
    print("=" * 46)
    print("  KTV ready")
    print("=" * 46)
    print()
    print(f"  Display:\n    {config.PUBLIC_URL or f'http://localhost:{config.PORT}'}/display")
    print()
    print(f"  Join:\n    {join_url}")
    print()
    try:
        import qrcode

        qr = qrcode.QRCode(border=2)
        qr.add_data(join_url)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        print("  [QR]  scan with your phone:")
        print()
        for row in matrix:
            print("   " + "".join("##" if cell else "  " for cell in row))
    except Exception as exc:  # QR is a nicety, never a blocker
        print(f"  (QR unavailable: {exc})")
    print()


def set_admin_password() -> None:
    import getpass

    from ktv.auth import MIN_PASSWORD, AuthStore

    password = getpass.getpass(f"New admin password (at least {MIN_PASSWORD} characters): ")
    if password != getpass.getpass("Repeat: "):
        sys.exit("Passwords do not match.")
    try:
        AuthStore(config.DB_PATH).set_admin_password(password)
    except ValueError as exc:
        sys.exit(str(exc))
    print(f"Admin password saved to {config.DB_PATH}.")
    print("Restart the service to sign out admin devices that used the old password.")


def main() -> None:
    parser = argparse.ArgumentParser(description="LocalKTV server")
    parser.add_argument("--host", default=config.HOST)
    parser.add_argument("--port", type=int, default=config.PORT)
    parser.add_argument("--no-browser", action="store_true", help="do not open /display")
    parser.add_argument("--set-admin-password", action="store_true",
                        help="set the admin password for the login gate, then exit")
    args = parser.parse_args()
    if args.set_admin_password:
        set_admin_password()
        return
    config.PORT = args.port  # banner, join URL and /api/qr.svg follow --port
    logsetup.setup()

    config.ensure_dirs()
    store = Store()
    cache = TrackCache()
    rooms = Rooms(config.DB_PATH, store, cache)
    pipeline = Pipeline(rooms.main, store, cache)
    rooms.attach_pipeline(pipeline)
    app = create_app(rooms.main, pipeline, cache, rooms=rooms)

    print_banner(config.join_url())

    if not args.no_browser:
        import threading
        import webbrowser

        threading.Timer(1.5, lambda: webbrowser.open(f"http://localhost:{args.port}/display")).start()

    # Only the local reverse proxy may supply forwarded scheme/client headers.
    # log_config=None: uvicorn's loggers go through logsetup's format.
    uvicorn.run(app, host=args.host, port=args.port, log_config=None,
                log_level=logsetup.level_from_env().lower(), forwarded_allow_ips="127.0.0.1")


if __name__ == "__main__":
    main()
