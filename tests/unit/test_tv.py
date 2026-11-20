import unittest

import tv

XML = ("<root><device><deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>"
       "<friendlyName>Salon (00:11:E1:5b:3f:32)</friendlyName><manufacturer>Acme</manufacturer><modelName>X1</modelName>"
       "<serviceList><service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType>"
       "<controlURL>/aw/AVT/control</controlURL></service></serviceList></device></root>")


class Description(unittest.TestCase):
    def setUp(self):
        self.d = tv.parse_description(XML, "http://10.0.0.5:61726/aw")

    def test_name_without_mac_address_and_absolute_control_address(self):
        self.assertEqual(self.d["name"], "Salon")
        self.assertEqual(self.d["services"]["AVTransport"], ("urn:schemas-upnp-org:service:AVTransport:1", "http://10.0.0.5:61726/aw/AVT/control"))

    def test_a_device_that_is_not_a_tv_is_left_out(self):
        self.assertTrue(tv.summarize([self.d])["services"])
        self.assertIsNone(tv.summarize([{**self.d, "services": {}, "type": "urn:x:device:Printer:1"}]))

    def test_a_tv_with_apps_but_no_player_is_still_described(self):
        only_dial = tv.summarize([{**self.d, "services": {}, "type": "urn:schemas-upnp-org:device:tvdevice:1", "app_url": "http://10.0.0.5:1/apps/"}])
        self.assertEqual(only_dial["services"], {})
        self.assertTrue(only_dial["app_url"])
