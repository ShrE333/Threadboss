from app.config import Settings
from app.media_processor import MediaProcessor


def test_localhost_media_url_rewritten_to_waha_origin():
    settings = Settings(
        waha_base_url='https://demo-waha.example.com',
        session_tenants_json='{"default":"tenant"}',
    )
    processor = MediaProcessor(settings)
    result = processor._rewrite_media_url('http://localhost:3000/api/files/a.ogg')
    assert result == 'https://demo-waha.example.com/api/files/a.ogg'


def test_remote_media_url_is_unchanged():
    settings = Settings(
        waha_base_url='https://demo-waha.example.com',
        session_tenants_json='{"default":"tenant"}',
    )
    processor = MediaProcessor(settings)
    url = 'https://cdn.example.com/file.pdf'
    assert processor._rewrite_media_url(url) == url
