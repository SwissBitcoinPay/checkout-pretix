import qrcode
import qrcode.exceptions
from django.utils.html import format_html
from qrcode.constants import ERROR_CORRECT_M

# Modules of white around the code, as the QR code standard asks for.
QUIET_ZONE = 4


def svg(text: str, label: str) -> str | None:
    """`text` as a QR code: an inline SVG image, made on the server (no script, no request).
    None when the text is too long for a QR code (the page shows it as text then).

    Dark modules are drawn as one path of horizontal runs, on a white background of its own,
    so that it scans whatever the page's colors.
    """
    code = qrcode.QRCode(error_correction=ERROR_CORRECT_M, border=QUIET_ZONE)
    code.add_data(text)
    try:
        code.make(fit=True)
    except (ValueError, qrcode.exceptions.DataOverflowError):
        return None
    matrix = code.get_matrix()
    size = len(matrix)
    runs = []
    for y, row in enumerate(matrix):
        x = 0
        while x < size:
            if not row[x]:
                x += 1
                continue
            start = x
            while x < size and row[x]:
                x += 1
            runs.append(f"M{start} {y}h{x - start}v1h-{x - start}z")
    image: str = format_html(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {0} {0}" role="img" '
        'aria-label="{1}" shape-rendering="crispEdges">'
        '<rect width="{0}" height="{0}" fill="#fff"/><path fill="#000" d="{2}"/></svg>',
        size,
        label,
        "".join(runs),
    )
    return image
