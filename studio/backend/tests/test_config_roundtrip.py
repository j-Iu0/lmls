"""Public config serialization must preserve executable graphs and editor extensions."""
from pathlib import Path

import pytest

from livesub.core.config import config_from_dict, config_to_dict, load_config
from livesub.core.registry import resolve
from lmls_studio.configuration import export_document, from_editor, import_document, to_editor

REPO = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize('path', sorted((REPO / 'config').glob('*.toml')))
def test_existing_presets_roundtrip(path):
    cfg = load_config(path)
    rebuilt = config_from_dict(config_to_dict(cfg))
    assert rebuilt.nodes == cfg.nodes
    assert rebuilt.settings == cfg.settings


@pytest.mark.parametrize('format', ['toml', 'json'])
def test_editor_roundtrip_fanin_unknown_settings_and_disabled(format):
    if format == 'toml':
        pytest.importorskip('tomli_w')
    cfg = load_config(REPO / 'config/mock.toml')
    cfg.settings['custom_extension'] = {'nested': {'value': [1, 2, 3]}}
    doc = to_editor(cfg)
    doc['editor']['positions'] = {'src': {'x': -22.5, 'y': 431}}
    doc['nodes'][0]['enabled'] = False
    result = import_document(export_document(doc, format), format)
    assert from_editor(result).nodes == from_editor(doc).nodes
    assert result['settings'] == doc['settings']
    assert result['editor'] == doc['editor']


def test_serializer_does_not_alias_options():
    cfg = load_config(REPO / 'config/mock.toml')
    raw = config_to_dict(cfg)
    raw['node'][0]['path'] = 'changed.wav'
    assert cfg.nodes[0].options['path'] == 'assets/lecture.wav'


def test_toml_omits_semantically_default_null_but_explains_other_nulls():
    pytest.importorskip('tomli_w')
    from livesub.core.config import ConfigError
    doc = to_editor(load_config(REPO / 'config/mock.toml'))
    asr = next(n for n in doc['nodes'] if n['impl'] == 'mock_transcriber')
    asr['options']['script'] = None
    rebuilt = import_document(export_document(doc, 'toml'), 'toml')
    assert 'script' not in next(n for n in rebuilt['nodes'] if n['impl'] == 'mock_transcriber')['options']
    asr['options']['custom'] = {'value': None}
    with pytest.raises(ConfigError, match='export JSON'):
        export_document(doc, 'toml')
    assert import_document(export_document(doc, 'json'), 'json')['nodes'] == doc['nodes']


def test_removed_first_fanin_connection_normalizes_remaining_synthetic_port():
    doc = to_editor(load_config(REPO / 'config/mock.toml'))
    screen = next(n for n in doc['nodes'] if n['name'] == 'screen')
    port = next(iter(resolve('stdout_pretty').inputs))
    screen['inputs'] = {port + '_1': 'text.out'}
    cfg = from_editor(doc)
    assert cfg.node('screen').in_topics == ['text.out']
    assert list(cfg.node('screen').inputs) == [port]
