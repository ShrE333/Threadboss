import io

from PIL import Image

from app.tools import ToolEngine


def test_qr_generation_returns_png_bytes():
    # Test the deterministic local utility without Redis/WAHA dependencies.
    engine = object.__new__(ToolEngine)
    data = engine._make_qr('https://example.com')
    assert data.startswith(b'\x89PNG')


def test_image_resize():
    img = Image.new('RGB', (20, 10), 'white')
    src = io.BytesIO()
    img.save(src, format='PNG')
    data, name, mime = ToolEngine._resize_image(src.getvalue(), 'x.png', 8, 8)
    out = Image.open(io.BytesIO(data))
    assert out.size == (8, 8)
    assert name.endswith('.png')
    assert mime == 'image/png'
