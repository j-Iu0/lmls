"""Public config serialization must preserve executable graphs and editor extensions."""
from pathlib import Path

import pytest

from livesub.core.config import config_from_dict, config_to_dict, load_config
from livesub.web.configuration import export_document, from_editor, import_document, to_editor


@pytest.mark.parametrize('path', sorted(Path('config').glob('*.toml')))
def test_existing_presets_roundtrip(path):
    cfg = load_config(path)
    rebuilt = config_from_dict(config_to_dict(cfg))
    assert rebuilt.nodes == cfg.nodes
    assert rebuilt.settings == cfg.settings


@pytest.mark.parametrize('format', ['toml', 'json'])
def test_editor_roundtrip_fanin_unknown_settings_and_disabled(format):
    if format == 'toml':
        pytest.importorskip('tomli_w')
    cfg = load_config('config/mock.toml')
    cfg.settings['custom_extension'] = {'nested': {'value': [1, 2, 3]}}
    doc = to_editor(cfg)
    doc['editor']['positions'] = {'src': {'x': -22.5, 'y': 431}}
    doc['nodes'][0]['enabled'] = False
    result = import_document(export_document(doc, format), format)
    assert from_editor(result).nodes == from_editor(doc).nodes
    assert result['settings'] == doc['settings']
    assert result['editor'] == doc['editor']


def test_serializer_does_not_alias_options():
    cfg = load_config('config/mock.toml')
    raw = config_to_dict(cfg)
    raw['node'][0]['path'] = 'changed.wav'
    assert cfg.nodes[0].options['path'] == 'assets/lecture.wav'
