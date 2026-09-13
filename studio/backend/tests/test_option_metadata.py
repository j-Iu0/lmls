"""Forwarded options remain usable by any config tool without constructing a module."""
from unittest.mock import patch

from lmls.core.registry import option_parameters, resolve
from lmls_studio.configuration import catalog


def test_segmenters_expose_real_forwarded_options():
    energy = option_parameters(resolve('energy'))
    assert energy['silence_ms'].default == 220
    assert energy['start_db'].default == 10
    assert 'config' not in energy
    silero = option_parameters(resolve('silero'))
    assert 'silence_ms' in silero and 'threshold' in silero
    assert silero['onnx'].default is False
    descriptions = {entry['impl']: entry for entry in catalog()}
    assert any(option['name'] == 'silence_ms' for option in descriptions['energy']['options'])


def test_ffmpeg_pulse_option_offers_monitor_and_discovered_sources():
    with patch('lmls.input.ffmpeg_source.FfmpegSource.list_pulse_sources',
               return_value=[{'kind': 'source', 'name': 'alsa_output.spk.monitor',
                              'monitor': True},
                             {'kind': 'sink', 'name': 'alsa_output.spk',
                              'monitor': False}]):
        descriptions = {entry['impl']: entry for entry in catalog()}
    pulse = next(
        option for option in descriptions['ffmpeg']['options'] if option['name'] == 'pulse'
    )
    assert pulse['choices'] == ['monitor', 'alsa_output.spk.monitor']
    # Options without an enumerator stay free-form.
    assert all('choices' not in option
               for option in descriptions['ffmpeg']['options'] if option['name'] != 'pulse')


def test_explicit_options_override_forwarded_defaults():
    class Forwarded:
        def __init__(self, rate: int = 1, optional: str = 'kept'): pass

    class Wrapper:
        option_sources = (Forwarded,)
        def __init__(self, rate: int = 2, **kwargs): pass

    options = option_parameters(Wrapper)
    assert options['rate'].default == 2
    assert options['optional'].default == 'kept'
