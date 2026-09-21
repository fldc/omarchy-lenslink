import unittest
from unittest.mock import Mock, patch
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lenslink_backend as b
from obs_control import Obs, ObsError, transform_value

class Controls(unittest.TestCase):
    def setUp(self):
        self.status = dict(connected=True, standby=False, screen=False)
        self.camera = dict(zoom=1, maxZoom=3, exposureBias=0, focusMode='auto', lensPosition=.5, lenses=['Front'])
    def run_control(self, command, value=None):
        with patch.object(b, 'live_state', return_value=(7,self.status,self.camera)), patch.object(b, 'selected_source', return_value=7), patch.object(b,'api') as api:
            result = b.control(command,value)
            return api, result
    def test_target_and_payload(self):
        api, result = self.run_control('zoom',2)
        api.assert_called_once_with('control',7,{'cmd':'zoom','value':2})
        self.assertIn('queued', result['message'])
    def test_invalid_zoom(self):
        for value in [True, float('nan'), float('inf'), 4, 0, '2']:
            with self.assertRaises(ObsError): self.run_control('zoom',value)
    def test_no_invented_effects(self):
        for command in ['portrait','studio_light','set_format','green_screen']:
            with self.assertRaises(ObsError): self.run_control(command,True)
    def test_missing_readback(self):
        self.camera = {}
        with self.assertRaises(ObsError): self.run_control('zoom',1)
    def test_disconnected(self):
        self.status['connected'] = False
        with self.assertRaises(ObsError): self.run_control('zoom',1)
    def test_start_only_standby(self):
        with self.assertRaises(ObsError): self.run_control('start_stream')
        self.status['standby']=True
        api,_=self.run_control('start_stream')
        api.assert_called_once_with('control',7,{'cmd':'start_stream'})
    def test_registry_safety(self):
        source=dict(id=7,name=b.SOURCE,screen=False)
        for sources in [[],[source,dict(id=8,name='Other',screen=False)], [dict(id=7,name='Anker',screen=False)]]:
            with patch.object(b,'api',return_value={'sources':sources}), self.assertRaises(ObsError): b.selected_source()
        with patch.object(b,'api',return_value={'sources':[source]}): self.assertEqual(b.selected_source(),7)
    def test_changed_source(self):
        with patch.object(b,'live_state',return_value=(7,self.status,self.camera)),patch.object(b,'selected_source',return_value=8),patch.object(b,'api') as api:
            with self.assertRaises(ObsError): b.control('zoom',2)
            api.assert_not_called()
    def test_partial_status(self):
        with patch.object(b,'live_state',side_effect=OSError),patch.object(b,'Obs',side_effect=OSError):
            state=b.state()
            self.assertFalse(state['connected']); self.assertFalse(state['obs']['available'])

class ObsIsolation(unittest.TestCase):
    def test_camera_item_exact_match(self):
        obs=Obs.__new__(Obs)
        for name,kind in [('Anker C200','v4l2_input'),(b.SOURCE,'v4l2_input')]:
            obs.request=Mock(side_effect=[{'currentProgramSceneName':'Anker'},{'sceneItems':[dict(sourceName=name,inputKind=kind,sceneItemId=1)]}])
            with self.assertRaises(ObsError):obs.camera_item()
    def test_no_start_wrong_scene(self):
        obs=Mock()
        obs.camera_item.side_effect=ObsError('wrong scene')
        with patch.object(b,'Obs',return_value=obs),patch.object(b,'live_state',return_value=(7,{'connected':True,'standby':False},{})):
            with self.assertRaises(ObsError):b.obs_action('start_virtual',[])
            obs.request.assert_not_called()
            obs.ws.close.assert_called_once()
    def test_poll_does_not_mutate_obs(self):
        obs=Mock()
        obs.request.side_effect=[{'currentProgramSceneName':b.SCENE},{'outputActive':False}]
        obs.camera_item.return_value=(b.SCENE,1,b.SOURCE)
        obs.transform.return_value={'sourceWidth':1920,'sourceHeight':1080}
        b.obs_status(obs)
        self.assertEqual([c.args[0] for c in obs.request.call_args_list],['GetCurrentProgramScene','GetVirtualCamStatus'])

    def test_missing_virtual_camera_does_not_hide_obs_or_connection(self):
        obs=Mock()
        obs.request.side_effect=[{'currentProgramSceneName':'Other'},ObsError('unsupported'),
            {'inputKind':'ios_camera_source','inputSettings':{'mode':'usb','host':'192.168.1.42'}}]
        status=b.obs_status(obs)
        self.assertTrue(status['available'])
        self.assertFalse(status['virtualSupported'])
        status['connection']=b.connection_settings(obs)
        self.assertEqual(status['connection']['mode'],'usb')

class Preview(unittest.TestCase):
    def test_preview_only_reads(self):
        obs=Mock()
        obs.request.side_effect=[{'currentProgramSceneName':b.SCENE},{'imageData':'data:image/jpeg;base64,YQ=='}]
        with patch.object(b,'Obs',return_value=obs):
            self.assertEqual(b.obs_action('preview',[])['scene'],b.SCENE)
        self.assertEqual([c.args[0] for c in obs.request.call_args_list],['GetCurrentProgramScene','GetSourceScreenshot'])
        obs.ws.close.assert_called_once()
    def test_invalid_preview_rejected(self):
        for value in ['file:///tmp/frame.jpg', 'data:image/jpeg;base64,'+'A'*120000]:
            obs=Mock()
            obs.request.side_effect=[{'currentProgramSceneName':b.SCENE},{'imageData':value}]
            with patch.object(b,'Obs',return_value=obs), self.assertRaises(ObsError):
                b.obs_action('preview',[])

class Framing(unittest.TestCase):
    def setUp(self): self.frame = dict(sourceWidth=1920,sourceHeight=1080,cropLeft=0,cropRight=0,cropTop=0,cropBottom=0)
    def test_zoom_creates_draggable_crop(self):
        crop=b.zoom_crop(self.frame,2)
        self.assertEqual(crop,dict(cropLeft=480,cropRight=480,cropTop=270,cropBottom=270))
        self.frame.update(crop)
        moved=b.drag_crop(self.frame,100,0,480,270)
        self.assertEqual(moved['cropLeft'],280)
        self.assertEqual(moved['cropRight'],680)
        self.assertEqual(moved['cropLeft']+moved['cropRight'],960)
    def test_pan_clamps_and_zoom_out_resets(self):
        self.frame.update(b.zoom_crop(self.frame,3.5))
        moved=b.drag_crop(self.frame,10000,-10000,480,270)
        self.assertEqual(moved['cropLeft'],0)
        self.assertEqual(moved['cropBottom'],0)
        self.frame.update(moved)
        self.assertTrue(all(v==0 for v in b.zoom_crop(self.frame,1).values()))
    def test_no_crop_gives_actionable_error(self):
        with self.assertRaisesRegex(ObsError,'framing zoom'): b.drag_crop(self.frame,10,10,480,270)
    def test_bad_zoom_rejected(self):
        for value in [0,11,True,float('nan')]:
            with self.assertRaises(ObsError):b.zoom_crop(self.frame,value)

    def test_rotation_preserves_frame_position(self):
        transform = dict(self.frame, rotation=0, alignment=5, positionX=0,
                         positionY=0, width=1920, height=1080,
                         boundsWidth=1920, boundsHeight=1080,
                         boundsType='OBS_BOUNDS_SCALE_INNER')
        self.assertEqual(b.orientation_update(transform),
                         {'rotation':180, 'positionX':1920, 'positionY':1080,
                          'boundsWidth':1920, 'boundsHeight':1080,
                          'boundsType':'OBS_BOUNDS_SCALE_INNER'})
        transform.update(rotation=180, positionX=1920, positionY=1080)
        self.assertEqual(b.orientation_update(transform),
                         {'rotation':0, 'positionX':0, 'positionY':0,
                          'boundsWidth':1920, 'boundsHeight':1080,
                          'boundsType':'OBS_BOUNDS_SCALE_INNER'})

    def test_portrait_is_centered_and_swaps_bounds(self):
        transform = dict(self.frame, rotation=0, alignment=5, positionX=0,
                         positionY=0, width=1920, height=1080,
                         boundsWidth=1920, boundsHeight=1080,
                         boundsType='OBS_BOUNDS_SCALE_INNER')
        self.assertEqual(b.orientation_update(transform, 90),
                         {'rotation':90, 'positionX':1920, 'positionY':0,
                          'boundsWidth':1080, 'boundsHeight':1920,
                          'boundsType':'OBS_BOUNDS_SCALE_INNER'})
        transform.update(rotation=90, positionX=1920, positionY=0,
                         boundsWidth=1080, boundsHeight=1920)
        self.assertEqual(b.orientation_update(transform, 270),
                         {'rotation':270, 'positionX':0, 'positionY':1080,
                          'boundsWidth':1080, 'boundsHeight':1920,
                          'boundsType':'OBS_BOUNDS_SCALE_INNER'})
        expected = b.orientation_update(dict(transform, rotation=0,
                                              boundsWidth=1920, boundsHeight=1080), 90)
        self.assertTrue(b.orientation_confirmed(dict(expected, positionX=1920.25), expected))
        self.assertFalse(b.orientation_confirmed(dict(expected, positionX=1921), expected))

    def test_custom_rotation_or_alignment_is_rejected(self):
        base = dict(self.frame, rotation=90, alignment=5, positionX=0,
                    positionY=0, width=1920, height=1080,
                    boundsWidth=1920, boundsHeight=1080,
                    boundsType='OBS_BOUNDS_SCALE_INNER')
        base['rotation'] = 45
        with self.assertRaisesRegex(ObsError, 'right angle'):
            b.orientation_update(base)
        base.update(rotation=0, alignment=0)
        with self.assertRaisesRegex(ObsError, 'top-left'):
            b.orientation_update(base)

    def test_rotation_action_checks_readback_and_crop(self):
        original = dict(self.frame, rotation=0, alignment=5, positionX=0,
                        positionY=0, width=1920, height=1080,
                        boundsWidth=1920, boundsHeight=1080,
                        boundsType='OBS_BOUNDS_SCALE_INNER')
        update = {'rotation':180, 'positionX':1920, 'positionY':1080,
                  'boundsWidth':1920, 'boundsHeight':1080,
                  'boundsType':'OBS_BOUNDS_SCALE_INNER'}
        actual = dict(original, **update)
        obs = Mock()
        obs.camera_item.return_value = (b.SCENE, 1, b.SOURCE)
        obs.transform.side_effect = [original, actual]
        with patch.object(b, 'Obs', return_value=obs):
            result = b.obs_action('rotate_180', [])
        obs.set_orientation.assert_called_once_with(b.SCENE, 1, update)
        self.assertIn('flipped', result['message'])

    def test_orientation_initializes_disabled_bounds(self):
        transform = dict(self.frame, rotation=0, alignment=5, positionX=0,
                         positionY=0, width=1920, height=1080,
                         boundsWidth=0, boundsHeight=0,
                         boundsType='OBS_BOUNDS_NONE')
        self.assertEqual(b.orientation_update(transform, 90),
                         {'rotation':90, 'positionX':1920, 'positionY':0,
                          'boundsWidth':1080, 'boundsHeight':1920,
                          'boundsType':'OBS_BOUNDS_SCALE_INNER'})

    def test_malformed_obs_orientation_is_rejected(self):
        for key, value in [('rotation', '180'), ('positionX', True),
                           ('width', 0), ('alignment', 5.5)]:
            with self.subTest(key=key), self.assertRaises(ObsError):
                transform_value({key:value})

    def test_disabled_obs_bounds_are_accepted(self):
        transform_value({'boundsWidth': 0.0, 'boundsHeight': 0.0})

class AdvancedControls(unittest.TestCase):
    run_control = Controls.run_control
    def setUp(self):
        Controls.setUp(self)
        self.camera.update(supportsManualExposure=True,minISO=20,maxISO=1920,minShutterSeconds=.000039,maxShutterSeconds=1/30,supportsWhiteBalanceLock=True,hasFlashlight=False,frameRates=[30,60])
    def test_manual_payloads(self):
        api,_=self.run_control('exposure',{'mode':'manual','iso':400})
        api.assert_called_once_with('control',7,{'cmd':'exposure','mode':'manual','iso':400})
        api,_=self.run_control('white_balance',{'mode':'locked','temperature':5500})
        api.assert_called_once_with('control',7,{'cmd':'white_balance','mode':'locked','temperature':5500})
    def test_bad_manual_values_rejected(self):
        for cmd,value in [('exposure',{'mode':'manual','iso':1921}),('exposure',{'mode':'manual','shutterSeconds':1}),('white_balance',{'mode':'locked','temperature':10000}),('flashlight',True),('set_format',{'fps':120})]:
            with self.assertRaises(ObsError):self.run_control(cmd,value)
    def test_manual_focus_and_format(self):
        api,_=self.run_control('focus_position',.3)
        api.assert_called_once_with('control',7,{'cmd':'focus','mode':'locked','lensPosition':.3})
        api,_=self.run_control('set_format',{'fps':30})
        api.assert_called_once_with('control',7,{'cmd':'set_format','fps':30})

class ViewConnection(unittest.TestCase):
    def test_reuses_connection_for_pan_and_zoom(self):
        obs=Mock()
        obs.request.return_value={'currentProgramSceneName':b.SCENE}
        obs.camera_item.return_value=(b.SCENE,1,b.SOURCE)
        transform=dict(sourceWidth=1920,sourceHeight=1080,cropLeft=480,cropRight=480,cropTop=270,cropBottom=270)
        obs.transform.return_value=transform
        result=b.view_request(obs,{'command':'pan','arguments':[20,10,480,270],'preview':False})
        self.assertIn('framing',result)
        result=b.view_request(obs,{'command':'zoom','arguments':[3],'preview':False})
        self.assertEqual(obs.set_crop.call_count,2)
        obs.ws.close.assert_not_called()
    def test_scene_switch_rejects_mutation(self):
        obs=Mock();obs.request.return_value={'currentProgramSceneName':'Anker'}
        with self.assertRaises(ObsError):b.view_request(obs,{'command':'zoom','arguments':[3],'preview':False})
        obs.set_crop.assert_not_called()
    def test_rejects_unknown_or_unbounded_arguments(self):
        for request in [{'command':'shell','arguments':[],'preview':False},{'command':'pan','arguments':[1]*5,'preview':False}]:
            with self.assertRaises(ObsError):b.view_request(Mock(),request)

class NetworkConnection(unittest.TestCase):
    def test_ipv6_and_mapped_ipv4_are_canonicalized(self):
        for host, expected in [('2001:db8:0:0::42', '2001:db8::42'), ('::ffff:192.168.1.42', '192.168.1.42')]:
            obs = Mock()
            obs.request.side_effect = [
                {'inputKind': 'ios_camera_source', 'inputSettings': {'mode': 'usb'}}, {},
                {'inputKind': 'ios_camera_source', 'inputSettings': {'mode': 'dial', 'host': expected}}]
            with patch.object(b, 'selected_source', return_value=7):
                b.set_connection(obs, [{'mode': 'dial', 'host': host}])
            self.assertEqual(obs.request.call_args_list[1].args[1]['inputSettings']['host'], expected)

    def test_ambiguous_source_registry_never_writes(self):
        obs = Mock()
        with patch.object(b, 'selected_source', side_effect=ObsError('Multiple sources')), self.assertRaises(ObsError):
            b.set_connection(obs, [{'mode': 'dial', 'host': '192.168.1.42'}])
        obs.request.assert_not_called()

    def test_wifi_updates_only_connection_fields_and_checks_readback(self):
        obs = Mock()
        obs.request.side_effect = [
            {'inputKind': 'ios_camera_source', 'inputSettings': {'mode': 'usb', 'usb_device': 'keep'}},
            {},
            {'inputKind': 'ios_camera_source', 'inputSettings': {'mode': 'dial', 'host': '192.168.1.42'}}]
        with patch.object(b, 'selected_source', return_value=7):
            result = b.set_connection(obs, [{'mode': 'dial', 'host': '192.168.1.42'}])
        self.assertIn('waiting', result['message'])
        self.assertEqual(obs.request.call_args_list[1].args, ('SetInputSettings', {
            'inputName': b.SOURCE, 'inputSettings': {'mode': 'dial', 'host': '192.168.1.42'}, 'overlay': True}))

    def test_usb_preserves_saved_wifi_address(self):
        obs = Mock()
        obs.request.side_effect = [
            {'inputKind': 'ios_camera_source', 'inputSettings': {'mode': 'dial', 'host': '192.168.1.2'}}, {},
            {'inputKind': 'ios_camera_source', 'inputSettings': {'mode': 'usb', 'host': '192.168.1.2'}}]
        with patch.object(b, 'selected_source', return_value=7):
            b.set_connection(obs, [{'mode': 'usb'}])
        self.assertEqual(obs.request.call_args_list[1].args[1]['inputSettings'], {'mode': 'usb'})

    def test_invalid_network_inputs_never_write(self):
        values = [{'mode': 'other'}, {'mode': 'usb', 'host': '192.168.1.2'}, {'mode': 'dial', 'host': '192.168.1.2', 'port': 80}]
        values += [{'mode': 'dial', 'host': h} for h in [None, True, 'http://192.168.1.2', '127.0.0.1', '0.0.0.0', '224.0.0.1', '::1', '::', 'ff02::1', '::ffff:127.0.0.1', '::ffff:0.0.0.0', '::ffff:224.0.0.1', 'bad;command', 'x'*65]]
        for value in values:
            obs = Mock()
            with self.subTest(value=value), self.assertRaises(ObsError):
                b.set_connection(obs, [value])
            obs.request.assert_not_called()

    def test_wrong_source_or_unconfirmed_settings_fail(self):
        obs = Mock()
        obs.request.return_value = {'inputKind': 'v4l2_input', 'inputSettings': {'mode': 'usb'}}
        with patch.object(b, 'selected_source', return_value=7), self.assertRaises(ObsError):
            b.set_connection(obs, [{'mode': 'usb'}])
        self.assertEqual(obs.request.call_count, 1)
        obs.request.return_value = {'inputKind': 'ios_camera_source', 'inputSettings': {'mode': 'usb'}}
        with patch.object(b, 'selected_source', return_value=7), self.assertRaisesRegex(ObsError, 'confirm'):
            b.set_connection(obs, [{'mode': 'dial', 'host': '192.168.1.2'}])

if __name__=='__main__':unittest.main()
