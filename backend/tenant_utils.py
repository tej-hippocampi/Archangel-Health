import secrets
import string


def generate_secure_password(length: int = 14) -> str:
    """Cryptographically secure password: mixed case, digits, symbols (min 12)."""
    length = max(length, 12)
    symbols = "!@#$%^&*-_"
    alphabet = string.ascii_letters + string.digits + symbols
    for _ in range(50):
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if (
            any(c.islower() for c in pwd)
            and any(c.isupper() for c in pwd)
            and any(c.isdigit() for c in pwd)
            and any(c in symbols for c in pwd)
        ):
            return pwd
    return pwd + "Aa1!"
