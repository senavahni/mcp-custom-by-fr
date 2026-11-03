"""ID pendek ber-prefix (hemat token saat dikirim ke LLM): base32 Crockford huruf kecil."""

import secrets

CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"
SUFFIX_LEN = 10


def _suffix(length: int = SUFFIX_LEN) -> str:
    return "".join(secrets.choice(CROCKFORD) for _ in range(length))


def new_handle() -> str:
    return f"h_{_suffix()}"


def new_job_id() -> str:
    return f"j_{_suffix()}"


def new_report_id() -> str:
    return f"r_{_suffix()}"


def new_mcp_run_id() -> str:
    return f"mr_{_suffix()}"


def is_short_id(value: str, prefix: str) -> bool:
    body = value.removeprefix(f"{prefix}_")
    return (
        value.startswith(f"{prefix}_")
        and len(body) == SUFFIX_LEN
        and all(c in CROCKFORD for c in body)
    )
