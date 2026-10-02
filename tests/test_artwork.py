import io
import json
import zipfile
import pytest
from PIL import Image
from malody_studio import artwork
from malody_studio.charts import Note, serialize, package

def jpeg(size=(640, 360)):
    data = io.BytesIO()
    Image.new('RGB', size, 'navy').save(data, format='JPEG')
    return data.getvalue()

def test_thumbnail_fallback_cache_and_checksum(monkeypatch, tmp_path):
    monkeypatch.setattr(artwork, 'ROOT', tmp_path)
    calls = []
    def get(request, timeout):
        calls.append(request.full_url)
        return io.BytesIO(jpeg((120, 90)) if len(calls) == 1 else jpeg())
    monkeypatch.setattr(artwork, 'urlopen', get)
    path, result = artwork.prepare_artwork('UKZt1vq8bKI')
    assert len(calls) == 2 and result['source'].endswith('hqdefault.jpg')
    assert path.read_bytes() == jpeg()
    assert artwork.prepare_artwork('UKZt1vq8bKI')[1]['sha256'] == result['sha256']
    assert len(calls) == 2

def test_invalid_image_or_cover_link_is_rejected():
    for value in (b'not JPEG', jpeg((120, 90))):
        with pytest.raises((ValueError, OSError)):
            artwork.check_jpeg(value)
    for value in ('http://youtu.be/UKZt1vq8bKI', 'https://example.com/a', 'https://youtu.be/../bad'):
        with pytest.raises(ValueError): artwork.video_id_from_url(value)
    assert artwork.video_id_from_url('https://youtu.be/UKZt1vq8bKI') == 'UKZt1vq8bKI'

def test_background_is_packaged_with_resolvable_mc_reference(tmp_path):
    background = tmp_path / 'cover.jpg'; background.write_bytes(jpeg())
    audio = tmp_path / 'source.ogg'; audio.write_bytes(b'OggS')
    chart = serialize([Note(1000, 0)], 't', 'a', 'Easy', 120)
    chart['meta']['background'] = 'background.jpg'
    archive = package(tmp_path, {'easy': chart}, audio, {'duration': 5}, background=background)
    with zipfile.ZipFile(archive) as z:
        reference = json.loads(z.read('0/easy.mc'))['meta']['background']
        assert z.read('0/' + reference) == jpeg()
    with pytest.raises(ValueError):
        package(tmp_path, {'easy': chart}, audio, {'duration': 5})
