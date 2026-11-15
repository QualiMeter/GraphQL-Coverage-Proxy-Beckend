"""Production Uvicorn entry point with an explicit, narrow proxy trust boundary."""

import argparse
import ipaddress
import os

import uvicorn


def trusted_proxy_ips() -> str:
    raw = os.environ.get("FORWARDED_ALLOW_IPS", "")
    parts = [part.strip() for part in raw.split(",")]
    if not all(parts):
        raise ValueError("FORWARDED_ALLOW_IPS must list the exact immediate proxy peer IPs")
    try:
        addresses = [ipaddress.ip_address(part) for part in parts]
    except ValueError:
        raise ValueError("FORWARDED_ALLOW_IPS accepts exact IPs only, never wildcard, networks or hostnames") from None
    if any(ip.is_unspecified or ip.is_multicast for ip in addresses):
        raise ValueError("FORWARDED_ALLOW_IPS contains an invalid proxy peer")
    return ",".join(dict.fromkeys(str(ip) for ip in addresses))


def server_config() -> uvicorn.Config:
    return uvicorn.Config(
        "app.main:app", host="0.0.0.0", port=8000,
        root_path=os.environ.get("API_ROOT_PATH", ""),
        proxy_headers=True, forwarded_allow_ips=trusted_proxy_ips(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-config", action="store_true", help="Validate proxy trust without starting or migrating")
    args = parser.parse_args()
    try:
        config = server_config()
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    if args.check_config:
        print("Production proxy trust configuration is valid")
    else:
        uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
