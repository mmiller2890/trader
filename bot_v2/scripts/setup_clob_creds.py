"""
Mint CLOB L2 API credentials and write them into .env.

Polymarket does not store these anywhere you can look them up. They are minted
on demand: the CLOB is sent a request signed by your private key, and it
returns an api key, secret and passphrase bound to *that EOA*. Creds issued to
one address are rejected with `401 Unauthorized/Invalid api key` when used by
another, which is the usual cause of a preflight that passes every public check
and fails only on `open_orders_read`.

    python3 -m scripts.setup_clob_creds --dry-run   # show the address first
    python3 -m scripts.setup_clob_creds

The three values are written straight into .env. They are never printed,
logged, or returned, so they do not reach your terminal scrollback or shell
history. The previous .env is copied to .env.bak first.

IMPORTANT: creating a key REPLACES the active key for that address. Anything
still using the old credentials stops working.

--derive re-fetches an existing key instead of minting a new one. Prefer it
when credentials already exist and you only need them locally; it does not
invalidate anything.
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

from config.loader import load_config

CRED_KEYS = ("CLOB_API_KEY", "CLOB_SECRET", "CLOB_PASSPHRASE")


def _upsert_env(path: Path, values: dict[str, str]) -> list[str]:
    """Replace or append each key in .env, leaving every other line untouched."""

    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    actions: list[str] = []

    for index, line in enumerate(lines):
        match = re.match(r"\s*([A-Z0-9_]+)\s*=", line)
        if match is None:
            continue
        key = match.group(1)
        if key in values:
            lines[index] = f"{key}={values[key]}"
            seen.add(key)
            actions.append(f"replaced {key}")

    for key, value in values.items():
        if key not in seen:
            lines.append(f"{key}={value}")
            actions.append(f"added {key}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return actions


def _paste_mode(env_path: Path) -> int:
    """
    Write credentials you already hold into .env.

    Each value is read with getpass, so nothing echoes to the terminal and
    nothing lands in shell history. Use this when the credentials already
    exist and only need to be restored -- there is no reason to mint new ones
    and invalidate the working set.
    """

    import sys
    from getpass import GetPassWarning, getpass

    prompts = (
        ("PRIVATE_KEY", "Private key"),
        ("CLOB_API_KEY", "API key"),
        ("CLOB_SECRET", "Secret"),
        ("CLOB_PASSPHRASE", "Passphrase"),
    )
    print("Paste each value and press Enter. Input is hidden.")
    print("Press Enter on its own to leave that entry unchanged.\n")
    sys.stdout.flush()

    values: dict[str, str] = {}
    for key, label in prompts:
        # getpass writes straight to the tty while print() buffers, so without
        # this flush the prompts appear out of order and the whole thing looks
        # like it hung on the wrong field.
        sys.stdout.flush()
        try:
            import warnings

            with warnings.catch_warnings():
                warnings.simplefilter("error", GetPassWarning)
                entered = getpass(f"  {label}: ").strip()
        except (GetPassWarning, Exception):  # noqa: BLE001
            # No usable tty (piped stdin, some IDE consoles). Fall back to a
            # visible read rather than silently reading nothing.
            print(f"  {label} (VISIBLE - no hidden input available): ", end="")
            sys.stdout.flush()
            entered = (sys.stdin.readline() or "").strip()
        if entered:
            values[key] = entered
            print(f"    ok - {len(entered)} characters")
        else:
            print("    skipped")
        sys.stdout.flush()

    if not values:
        print("\nnothing entered; .env unchanged.")
        print("If the prompts did not accept your paste, run with --visible.")
        return 0

    if env_path.exists():
        shutil.copyfile(env_path, env_path.with_suffix(".bak"))
        print(f"\nbacked up {env_path} -> {env_path.with_suffix('.bak')}")

    for action in _upsert_env(env_path, values):
        print(f"  {action}")
    env_path.chmod(0o600)
    print(f"\nwrote {len(values)} value(s) into {env_path} (mode 600).")
    print("Verify with:  python -m scripts.setup_clob_creds --dry-run")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--derive",
        action="store_true",
        help="re-fetch existing credentials instead of minting new ones",
    )
    parser.add_argument("--nonce", type=int, default=0)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the signing address and change nothing",
    )
    parser.add_argument(
        "--paste",
        action="store_true",
        help="prompt for credentials you already hold and write them to .env",
    )
    args = parser.parse_args(argv)

    if args.paste:
        return _paste_mode(args.env_file)

    config = load_config(args.config_dir)
    private_key = config.secrets.private_key
    if private_key is None:
        print("PRIVATE_KEY is not set in .env; nothing to sign with.")
        return 2

    from py_clob_client_v2 import ClobClient

    client = ClobClient(
        config.exchange.clob_host,
        key=private_key.get_secret_value(),
        chain_id=config.exchange.chain_id,
    )
    address = client.get_address()

    print(f"signing address   {address}")
    print(f"clob host         {config.exchange.clob_host}")
    print(f"signature_type    {config.exchange.signature_type}")
    print()
    print(
        "Credentials will be bound to the signing address above. If that is not\n"
        "the address your Polymarket account trades from, minting here will clear\n"
        "the 401 but orders will fail later at authorization instead."
    )
    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    try:
        creds = (
            client.derive_api_key(nonce=args.nonce)
            if args.derive
            else client.create_api_key(nonce=args.nonce)
        )
    except Exception as exc:  # noqa: BLE001 - surfaced verbatim for diagnosis
        verb = "derive" if args.derive else "create"
        print(f"\nfailed to {verb} api credentials: {type(exc).__name__}: {exc}")
        if args.derive:
            print("No key exists for this address yet; run without --derive to mint one.")
        return 1

    if not (creds.api_key and creds.api_secret and creds.api_passphrase):
        print("\nthe CLOB returned an incomplete credential set; nothing written.")
        return 1

    env_path = args.env_file
    if env_path.exists():
        shutil.copyfile(env_path, env_path.with_suffix(".bak"))
        print(f"\nbacked up {env_path} -> {env_path.with_suffix('.bak')}")

    actions = _upsert_env(
        env_path,
        {
            "CLOB_API_KEY": creds.api_key,
            "CLOB_SECRET": creds.api_secret,
            "CLOB_PASSPHRASE": creds.api_passphrase,
        },
    )
    env_path.chmod(0o600)

    for action in actions:
        print(f"  {action}")
    print(f"\nwrote credentials for {address} into {env_path} (mode 600).")
    print("Re-run preflight from the dashboard; open_orders_read should now pass.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
