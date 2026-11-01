"""Small presentation helpers for the TOTP screens (QR rendering is server-side,
so no third-party script ever sees the secret)."""
import segno


def qr_data_uri(otpauth_uri):
    """The otpauth:// URI as an inline SVG QR code (data: URI) for an <img>."""
    return segno.make(otpauth_uri, error="m").svg_data_uri(scale=5, border=2)


def group_secret(secret):
    """'ABCDEFGH...' -> 'ABCD EFGH ...' for easier manual entry."""
    return " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))
