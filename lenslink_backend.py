#!/usr/bin/python3 -I
"""Local LensLink v1.10.0 control bridge; no writes during polling."""
import http.client
import ipaddress
import json
import math
import re
import signal
import sys
import time
from pathlib import Path
if __name__ == '__main__':
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from obs_control import Obs, ObsError
from secure_io import json_object

SOURCE = 'iPhone LensLink USB'
SCENE = 'iPhone LensLink'


def api(path, source=None, payload=None):
    connection = http.client.HTTPConnection('127.0.0.1', 9980, timeout=1.5)
    try:
        url = '/api/' + path + (f'?src={source}' if source is not None else '')
        connection.request('POST' if payload is not None else 'GET', url,
                           json.dumps(payload) if payload is not None else None,
                           {'Content-Type': 'application/json'})
        response = connection.getresponse()
        raw = response.read(16385)
        if response.status not in (200, 204):
            raise ObsError('LensLink API unavailable')
        return json_object(raw) if raw else {}
    finally:
        connection.close()


def selected_source():
    sources = api('sources').get('sources', [])
    if type(sources) is not list:
        raise ObsError('Invalid LensLink source list')
    matches = [s for s in sources if type(s) is dict and s.get('name') == SOURCE
               and s.get('screen') is False]
    if len(matches) != 1 or type(matches[0].get('id')) is not int:
        raise ObsError('Expected one iPhone LensLink USB camera source')
    # Upstream falls back to its first source for stale IDs. Refuse multi-source
    # registries rather than risk targeting a different phone during a race.
    if len(sources) != 1:
        raise ObsError('Multiple LensLink sources: use the LensLink browser panel')
    return matches[0]['id']


def numeric(value, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ObsError('Camera value outside supported range')
    return value


def live_state():
    source = selected_source()
    status = api('status', source)
    for flag in ('connected', 'standby', 'screen'):
        if type(status.get(flag)) is not bool:
            raise ObsError('Invalid LensLink status')
    state = api('state', source) if status['connected'] and not status['standby'] else {}
    return source, status, state


def control(command, value=None):
    source, status, state = live_state()
    if command == 'autostart':
        if type(value) is not bool: raise ObsError('Auto-start requires a boolean')
        api('autostart', source, {'on': value})
        return {'message': 'Auto-start updated'}
    if command == 'recalibrate':
        if not status['connected']: raise ObsError('Phone is disconnected')
        api('recalibrate', source, {})
        return {'message': 'Lip-sync recalibration requested'}
    if command == 'start_stream':
        if not status['standby']: raise ObsError('Phone is not in standby')
        payload = {'cmd': command}
    else:
        if not status['connected'] or status['standby'] or status['screen']:
            raise ObsError('Phone camera is not streaming')
        payload = {'cmd': command}
        if command == 'stop_stream': pass
        elif command == 'zoom':
            numeric(state.get('zoom'), 1, 100)
            payload['value'] = numeric(value, 1, numeric(state.get('maxZoom'), 1, 100))
        elif command == 'exposure_bias':
            numeric(state.get('exposureBias'), -20, 20)
            payload['value'] = numeric(value, -2, 2)
        elif command == 'focus':
            if state.get('focusMode') not in ('auto', 'locked'): raise ObsError('Focus readback unavailable')
            if value not in ('auto', 'locked'): raise ObsError('Invalid focus mode')
            payload['mode'] = value
            if value == 'locked': payload['lensPosition'] = numeric(state.get('lensPosition'), 0, 1)
        elif command == 'focus_position':
            numeric(state.get('lensPosition'), 0, 1)
            payload = {'cmd': 'focus', 'mode': 'locked', 'lensPosition': numeric(value, 0, 1)}
        elif command == 'exposure':
            if state.get('supportsManualExposure') is not True:
                raise ObsError('Manual exposure unsupported')
            payload.update(validate_mode(value, ('auto', 'manual'), ('iso', 'shutterSeconds')))
            if payload['mode'] == 'manual':
                if 'iso' in payload:
                    numeric(payload['iso'], numeric(state.get('minISO'), 1, 100000), numeric(state.get('maxISO'), 1, 100000))
                if 'shutterSeconds' in payload:
                    numeric(payload['shutterSeconds'], numeric(state.get('minShutterSeconds'), 0.000001, 10), numeric(state.get('maxShutterSeconds'), 0.000001, 10))
        elif command == 'white_balance':
            if state.get('supportsWhiteBalanceLock') is not True:
                raise ObsError('White balance lock unsupported')
            payload.update(validate_mode(value, ('auto', 'locked'), ('temperature',)))
            if 'temperature' in payload: numeric(payload['temperature'], 2500, 8000)
        elif command == 'flashlight':
            if state.get('hasFlashlight') is not True or type(value) is not bool:
                raise ObsError('Flashlight unavailable')
            payload['on'] = value
        elif command == 'set_format':
            choices = {'resolution': 'resolutions', 'fps': 'frameRates', 'codec': 'codecs'}
            if type(value) is not dict or len(value) != 1 or next(iter(value)) not in choices:
                raise ObsError('Select one supported format field')
            key, setting = next(iter(value.items()))
            if type(setting) not in (str, int) or setting not in state.get(choices[key], []):
                raise ObsError('Format not offered by phone')
            payload[key] = setting
        elif command == 'mic':
            if not state.get('micEnabled') or type(value) is not str or value not in [m.get('id') for m in state.get('mics', []) if type(m) is dict]:
                raise ObsError('Microphone unavailable')
            payload['id'] = value
        elif command == 'green_screen':
            if state.get('supportsGreenScreen') is not True or type(value) is not dict or len(value) != 1:
                raise ObsError('Green screen unavailable')
            if 'on' in value and type(value['on']) is bool: payload['on'] = value['on']
            elif 'maxDistance' in value and state.get('greenScreenDepth') is True:
                payload['maxDistance'] = numeric(value['maxDistance'], 0, 5)
            else: raise ObsError('Unsupported green screen setting')
        elif command == 'selectLens':
            if type(value) is not str or value not in state.get('lenses', []): raise ObsError('Lens unavailable')
            payload['label'] = value
        else: raise ObsError('Unsupported camera command')
    if selected_source() != source: raise ObsError('Camera source changed; retry')
    api('control', source, payload)
    return {'message': 'Command queued by LensLink; waiting for phone readback'}


def validate_mode(value, modes, fields):
    if type(value) is not dict or value.get('mode') not in modes or set(value) - {'mode', *fields}:
        raise ObsError('Invalid camera mode settings')
    if value['mode'] == modes[0] and len(value) != 1:
        raise ObsError('Automatic mode takes no manual values')
    return value


def framing_state(transform):
    width = numeric(transform.get('sourceWidth'), 1, 32768)
    height = numeric(transform.get('sourceHeight'), 1, 32768)
    visible = width - transform.get('cropLeft', 0) - transform.get('cropRight', 0)
    rotation = numeric(transform.get('rotation', 0), -3600, 3600) % 360
    canonical = next((angle for angle in (0, 90, 180, 270) if abs(rotation-angle) < .01), None)
    return {'zoom': width / max(1, visible), 'width': width, 'height': height,
            'rotation': canonical,
            'crop': {key: transform.get(key, 0) for key in ('cropLeft','cropRight','cropTop','cropBottom')}}


def orientation_update(transform, target=None):
    rotation = numeric(transform.get('rotation', 0), -3600, 3600) % 360
    current = next((angle for angle in (0, 90, 180, 270) if abs(rotation-angle) < .01), None)
    if current is None:
        raise ObsError('Reset the source rotation to a right angle in OBS first')
    if target is None:
        target = (current + 180) % 360
    if type(target) not in (int, float) or target not in (0, 90, 180, 270):
        raise ObsError('Choose a supported screen orientation')
    if numeric(transform.get('alignment'), 0, 15) != 5:
        raise ObsError('Use top-left source alignment in OBS before rotating')
    bounds_type = transform.get('boundsType')
    if bounds_type == 'OBS_BOUNDS_SCALE_INNER':
        bound_w = numeric(transform.get('boundsWidth'), 1, 32768)
        bound_h = numeric(transform.get('boundsHeight'), 1, 32768)
    elif bounds_type == 'OBS_BOUNDS_NONE':
        bound_w = numeric(transform.get('width'), 1, 32768)
        bound_h = numeric(transform.get('height'), 1, 32768)
    else:
        raise ObsError('Use no bounds or Scale to inner bounds in OBS before rotating')
    canvas_w, canvas_h = max(bound_w, bound_h), min(bound_w, bound_h)
    target_w, target_h = (canvas_h, canvas_w) if target in (90, 270) else (canvas_w, canvas_h)
    # OBS applies crop and inner-bounds scaling before rotation. These four
    # canonical top-left origins keep the result in the landscape canvas;
    # swapped portrait bounds supply centered pillarboxing.
    positions = {0:(0, 0), 90:(canvas_w, 0),
                 180:(canvas_w, canvas_h), 270:(0, canvas_h)}
    x, y = positions[target]
    return {'rotation': target, 'positionX': x, 'positionY': y,
            'boundsWidth': target_w, 'boundsHeight': target_h,
            'boundsType': 'OBS_BOUNDS_SCALE_INNER'}


def orientation_confirmed(actual, expected):
    return all(actual.get(key) == value if key == 'boundsType' else
               abs(actual.get(key, float('inf')) - value)
               < (.51 if key in ('positionX','positionY') else .01)
               for key, value in expected.items())


def zoom_crop(transform, zoom):
    zoom = numeric(zoom, 1, 10)
    frame = framing_state(transform)
    width, height = int(frame['width']), int(frame['height'])
    visible_x, visible_y = max(1, round(width / zoom)), max(1, round(height / zoom))
    left, right = transform.get('cropLeft', 0), transform.get('cropRight', 0)
    top, bottom = transform.get('cropTop', 0), transform.get('cropBottom', 0)
    center_x = (left + width - right) / 2
    center_y = (top + height - bottom) / 2
    new_left = max(0, min(width-visible_x, round(center_x-visible_x/2)))
    new_top = max(0, min(height-visible_y, round(center_y-visible_y/2)))
    return dict(cropLeft=new_left, cropRight=width-visible_x-new_left,
                cropTop=new_top, cropBottom=height-visible_y-new_top)


def drag_crop(transform, dx, dy, width, height):
    frame = framing_state(transform)
    numeric(dx, -32768, 32768); numeric(dy, -32768, 32768)
    numeric(width, 1, 32768); numeric(height, 1, 32768)
    crop = frame['crop']
    total_x = crop['cropLeft'] + crop['cropRight']
    total_y = crop['cropTop'] + crop['cropBottom']
    if not total_x and not total_y: raise ObsError('Increase framing zoom above 1× before dragging')
    left = max(0, min(total_x, round(crop['cropLeft'] - dx*(frame['width']-total_x)/width)))
    top = max(0, min(total_y, round(crop['cropTop'] - dy*(frame['height']-total_y)/height)))
    return dict(cropLeft=left, cropRight=total_x-left, cropTop=top, cropBottom=total_y-top)


def obs_status(obs):
    scene = obs.request('GetCurrentProgramScene')['currentProgramSceneName']
    result = {'available': True, 'scene': scene, 'virtual': False,
              'virtualSupported': False}
    try:
        virtual = obs.request('GetVirtualCamStatus')['outputActive']
        if type(virtual) is not bool: raise ObsError('Invalid virtual camera status')
        result.update(virtual=virtual, virtualSupported=True)
    except (ObsError, KeyError):
        # Virtual camera is optional. Its absence must not hide otherwise
        # working OBS input settings, framing, preview, or USB/Wi-Fi switching.
        pass
    if scene == SCENE:
        try:
            scene, item, _ = obs.camera_item()
            result['framing'] = framing_state(obs.transform(scene, item))
        except ObsError: pass
    return result


def connection_settings(obs):
    data = obs.request('GetInputSettings', {'inputName': SOURCE})
    if data.get('inputKind') != 'ios_camera_source' or type(data.get('inputSettings')) is not dict:
        raise ObsError('Expected the existing LensLink camera source')
    settings = data['inputSettings']
    mode = settings.get('mode', 'dial')
    host = settings.get('host', '')
    if mode not in ('usb', 'dial') or type(host) is not str or len(host) > 144:
        raise ObsError('Invalid LensLink connection settings')
    return {'mode': mode, 'host': host}


def set_connection(obs, values):
    if len(values) != 1 or type(values[0]) is not dict:
        raise ObsError('Connection settings required')
    value = values[0]
    if set(value) - {'mode', 'host'} or value.get('mode') not in ('usb', 'dial'):
        raise ObsError('Choose USB or Wi-Fi')
    settings = {'mode': value['mode']}
    if value['mode'] == 'dial':
        host = value.get('host')
        if type(host) is not str or len(host) > 64:
            raise ObsError('Enter the IP address shown in LensLink')
        try:
            address = ipaddress.ip_address(host.strip())
        except ValueError as error:
            raise ObsError('Enter the IP address shown in LensLink') from error
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if address.is_loopback or address.is_unspecified or address.is_multicast or '%' in str(address):
            raise ObsError('Enter the phone network IP address')
        settings['host'] = str(address)
    elif 'host' in value:
        raise ObsError('USB does not require an IP address')
    # Verify both APIs refer to the one existing camera, even while disconnected.
    selected_source()
    connection_settings(obs)
    obs.request('SetInputSettings', {'inputName': SOURCE, 'inputSettings': settings, 'overlay': True})
    actual = connection_settings(obs)
    if any(actual.get(key) != expected for key, expected in settings.items()):
        raise ObsError('OBS did not confirm the connection settings')
    label = 'Wi-Fi' if settings['mode'] == 'dial' else 'USB'
    return {'message': label + ' selected; waiting for LensLink to connect', 'connection': actual}


def state():
    result = {'connected': False, 'standby': False, 'camera': {}, 'status': 'LensLink unavailable',
              'obs': {'available': False, 'virtual': False, 'scene': ''}}
    try:
        _, status, camera = live_state()
        result.update(connected=status['connected'], standby=status['standby'], camera=camera,
                      status=str(status.get('status', 'Idle'))[:256],
                      autoStart=status.get('autoStart', False), sync=status.get('sync', 'off'))
    except Exception as error:
        result['status'] = str(error)[:256] if isinstance(error, ObsError) else 'LensLink API unavailable'
    try:
        obs = Obs()
        try:
            result['obs'] = obs_status(obs)
            try: result['obs']['connection'] = connection_settings(obs)
            except ObsError: pass
        finally: obs.ws.close()
    except Exception:
        result['obs']['error'] = 'OBS WebSocket unavailable; open OBS and enable its WebSocket server'
    return result


def preview_payload(obs, scene):
    image = obs.request('GetSourceScreenshot', {'sourceName': scene, 'imageFormat': 'jpg',
                        'imageWidth': 640, 'imageCompressionQuality': 55})['imageData']
    if type(image) is not str or len(image) > 120000 or not re.fullmatch(r'data:image/(?:jpeg|jpg);base64,[A-Za-z0-9+/=]+', image):
        raise ObsError('Invalid preview image')
    return {'image': image, 'scene': scene}


def view_request(obs, request):
    if type(request) is not dict or set(request) - {'command', 'arguments', 'preview'}:
        raise ObsError('Invalid view request')
    command, args = request.get('command'), request.get('arguments', [])
    if command not in ('frame', 'pan', 'zoom') or type(args) is not list or len(args) > 4:
        raise ObsError('Invalid framing command')
    if type(request.get('preview')) is not bool: raise ObsError('Preview flag required')
    scene = obs.request('GetCurrentProgramScene')['currentProgramSceneName']
    result = {'scene': scene}
    if command != 'frame' and scene != SCENE:
        raise ObsError('Select the iPhone scene before reframing')
    if scene == SCENE:
        # Revalidate identity and transform so scene switches and external edits
        # cannot redirect a long-lived session onto another source.
        scene, item, _ = obs.camera_item()
        transform = obs.transform(scene, item)
        dimensions = ('sourceWidth', 'sourceHeight')
        if not all(transform.get(name, 0) > 0 for name in dimensions):
            if command != 'frame' or args:
                raise ObsError('LensLink video is still starting')
            return result
        if command == 'pan':
            if len(args) != 4: raise ObsError('Pan requires four values')
            crop = drag_crop(transform, *args)
            obs.set_crop(scene, item, crop)
            transform.update(crop)
        elif command == 'zoom':
            if len(args) != 1: raise ObsError('Zoom requires one value')
            crop = zoom_crop(transform, args[0])
            obs.set_crop(scene, item, crop)
            transform.update(crop)
        elif args: raise ObsError('Unexpected frame arguments')
        result['framing'] = framing_state(transform)
    if request['preview']: result.update(preview_payload(obs, scene))
    return result


def view_serve():
    obs = Obs()
    try:
        print(json.dumps({'ready': True}), flush=True)
        while True:
            raw = sys.stdin.buffer.readline(2049)
            if not raw: return 0
            if len(raw) > 2048 or not raw.endswith(b'\n'):
                raise ObsError('Framing request limit exceeded')
            # A request deadline; no lifetime timeout while waiting for input.
            signal.alarm(8)
            try:
                response = view_request(obs, json_object(raw, 2048))
            except ObsError as error:
                response = {'error': str(error)[:256]}
            finally: signal.alarm(0)
            print(json.dumps(response, allow_nan=False), flush=True)
    finally: obs.ws.close()


def obs_action(command, values):
    obs = Obs()
    try:
        if command == 'connection':
            return set_connection(obs, values)
        elif command == 'preview':
            scene = obs.request('GetCurrentProgramScene')['currentProgramSceneName']
            return preview_payload(obs, scene)
        elif command == 'convert_zoom':
            scene, item, _ = obs.camera_item()
            original_transform = obs.transform(scene, item)
            _, _, camera = live_state()
            original_zoom = numeric(camera.get('zoom'), 1, 10)
            crop = zoom_crop(original_transform, numeric(framing_state(original_transform)['zoom'] * original_zoom, 1, 10))
            try:
                control('zoom', 1)
                deadline = time.monotonic() + 2
                while live_state()[2].get('zoom') != 1:
                    if time.monotonic() >= deadline: raise ObsError('Phone did not confirm zoom reset')
                    time.sleep(.1)
                obs.set_crop(scene, item, crop)
            except Exception:
                # Best-effort rollback if either endpoint rejects conversion.
                control('zoom', original_zoom)
                obs.set_crop(scene, item, {k: original_transform.get(k, 0) for k in ('cropLeft','cropRight','cropTop','cropBottom')})
                raise
            return {'message': 'Lens zoom moved to OBS framing; drag the picture to position it'}
        elif command == 'select_scene':
            # Verify the existing scene and exact input; never create or rewrite it.
            items = obs.request('GetSceneItemList', {'sceneName': SCENE})['sceneItems']
            if not any(i.get('sourceName') == SOURCE and i.get('inputKind') == 'ios_camera_source' for i in items):
                raise ObsError('LensLink scene/source missing')
            obs.request('SetCurrentProgramScene', {'sceneName': SCENE})
        elif command in ('start_virtual', 'stop_virtual'):
            if command == 'start_virtual':
                _, status, _ = live_state()
                if not status['connected'] or status['standby']: raise ObsError('Phone is not streaming')
                obs.camera_item()  # Require iPhone in current program before broadcasting.
            active = obs.request('GetVirtualCamStatus')['outputActive']
            if active != (command == 'start_virtual'):
                obs.request('StartVirtualCam' if command == 'start_virtual' else 'StopVirtualCam')
        elif command in ('pan', 'center', 'framing_zoom'):
            scene, item, _ = obs.camera_item()
            transform = obs.transform(scene, item)
            if command == 'pan':
                if len(values) != 4: raise ObsError('Pan requires four values')
                crop = drag_crop(transform, *values)
            elif command == 'framing_zoom':
                if len(values) != 1: raise ObsError('Framing zoom requires one value')
                crop = zoom_crop(transform, values[0])
            else:
                x = int(transform.get('cropLeft', 0) + transform.get('cropRight', 0))
                y = int(transform.get('cropTop', 0) + transform.get('cropBottom', 0))
                crop = dict(cropLeft=x//2, cropRight=x-x//2, cropTop=y//2, cropBottom=y-y//2)
            obs.set_crop(scene, item, crop)
        elif command in ('rotate_180', 'orientation'):
            scene, item, _ = obs.camera_item()
            original = obs.transform(scene, item)
            if command == 'orientation':
                if len(values) != 1: raise ObsError('Choose one screen orientation')
                update = orientation_update(original, values[0])
            else:
                update = orientation_update(original)
            orientation_keys = ('rotation','positionX','positionY','boundsWidth','boundsHeight','boundsType')
            original_orientation = {key: original.get(key, 0) for key in orientation_keys}
            original_crop = {key: original.get(key, 0) for key in ('cropLeft','cropRight','cropTop','cropBottom')}
            obs.set_orientation(scene, item, update)
            actual = obs.transform(scene, item)
            confirmed = orientation_confirmed(actual, update)
            crop_preserved = all(actual.get(key, 0) == value for key, value in original_crop.items())
            if not confirmed or not crop_preserved:
                try: obs.set_orientation(scene, item, original_orientation)
                except Exception: pass
                raise ObsError('OBS did not confirm the orientation change')
            labels = {0:'Landscape', 90:'Portrait', 180:'Landscape flipped', 270:'Portrait flipped'}
            return {'message': labels[update['rotation']] + ' orientation selected'}
        else: raise ObsError('Unsupported OBS action')
        return {'message': 'OBS updated'}
    finally:
        obs.ws.close()


def main():
    signal.signal(signal.SIGALRM, lambda *_: (_ for _ in ()).throw(ObsError('Request timed out')))
    signal.alarm(12)
    try:
        if len(sys.argv) < 2: raise ObsError('Command required')
        command = sys.argv[1]
        if command == 'view_serve' and len(sys.argv) == 2:
            signal.alarm(0)
            return view_serve()
        args = [json.loads(x) for x in sys.argv[2:]]
        if command == 'state' and not args: result = state()
        elif command in ('zoom','exposure_bias','focus','focus_position','exposure','white_balance','flashlight','set_format','mic','green_screen','autostart','recalibrate','selectLens','start_stream','stop_stream'):
            if len(args) > 1: raise ObsError('Too many arguments')
            result = control(command, *args)
        else: result = obs_action(command, args)
        print(json.dumps(result, allow_nan=False))
        return 0
    except Exception as error:
        message = str(error) if isinstance(error, ObsError) else 'Local camera request failed'
        print(json.dumps({'error': message[:256]}))
        return 1

if __name__ == '__main__':
    sys.exit(main())
