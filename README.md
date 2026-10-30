# Godrej Aer Smart Matic Integration for Home Assistant

This is a custom integration for Home Assistant to support Godrej Aer Smart Matic, a Bluetooth Low Energy (BLE) room freshener device. Using this integration, you can monitor and control your Godrej Aer Smart Matic directly from Home Assistant.

## Supported Features

This integration currently supports the following features:

- Status sensor: Idle, Auto, Schedule or Disconnected (not heard, or the last exchange failed); the mode is from the last status read (see Updated last)
- Spray once via a `button` entity
- Auto-spray mode via a `select` entity: Off, or every 10, 20 or 40 minutes (attribute `started_by`: app, schedule or button)
- Identify (diagnostic button): lights the device's three interval lights for a few seconds. Works only while auto-spray is off and no schedule is running
- Reset the refill level via a `button` entity (after replacing the cartridge)
- Battery level as a percentage of what the device can use: the alkaline discharge curve, from 3.1 V (100 %) down to the device's 2.4 V cut-off (0 %); the measured voltage is a diagnostic sensor. Below 2.4 V the device still acknowledges sprays without spraying, so Spray now and Spray: auto show "Batteries are too low to spray"
- Estimated refill level (%) and lifetime spray count sensors
- Connection state via a `binary_sensor` entity
- On-demand status refresh via the `godrej_aer.update_status` service

## Services

### `godrej_aer.update_status`

Connects to the device and refreshes its status (battery voltage and
connection state), bypassing the automatic retry cooldown that applies to
background polling. Target one or more devices/entities, or call it without
a target to update every configured device.

```yaml
action: godrej_aer.update_status
target:
  device_id: <your Smart Matic device>
```

## Installation

### Manual Installation

1. **Download the Repository**: Clone or download this repository and place it within your Home Assistant `config` directory, making sure the final path is `/config/custom_components/godrej_aer`.

2. **Restart Home Assistant**: After adding the custom component to your setup, restart Home Assistant to ensure it finds the new integration.

3. **Add the Integration**: Navigate to the Home Assistant UI and go to **Settings > Devices & Services > Integrations** and look for "Godrej Aer Smart Matic". Follow the on-screen instructions to complete setup.

### Installation via HACS

1. **Open HACS**: Go to the Home Assistant UI, click on **HACS** in the sidebar, then click on **Integrations**.

2. **Add Custom Repository**: Click the three dots in the top right corner, then select **Custom repositories**.

3. **Enter the Repository URL**: Add `https://github.com/ov1d1u/godrej_aer` in the URL field, and select **Integration** as the category. Click **Add**.

4. **Install the Integration**: Search for "Godrej Aer Smart Matic" in HACS. Click on it, then click **Install**.

5. **Restart Home Assistant**: After installation, restart Home Assistant.

6. **Add the Integration through Home Assistant**: Navigate to **Settings > Devices & Services > Integrations** and look for "Godrej Aer Smart Matic". Follow the on-screen setup instructions.

## Configuration

To configure the Godrej Aer Smart Matic integration:

1. **Automatic Discovery**: The integration will attempt to automatically discover any Godrej Aer Smart Matic devices via Bluetooth.
   
2. **Manual MAC Address Entry**: If your device is not automatically discovered, you can manually enter the MAC address.

## Command and reply channels

The device takes commands on a characteristic of the `6e400000-…` service
and replies with notifications on `6e400001-…`. Firmware 3.0.8-IN accepts
commands on `6e400002-…`, `6e400003-…` and `6e400004-…` alike; other builds
have been reported to use `6e400003-…` only.

When you add a device, the integration checks the defaults (commands on
`6e400002-…`, replies on `6e400001-…`) with the read-only fetch-status
request. If they don't answer, it probes every writable characteristic of
the service, with every notifying one subscribed, and stores whichever pair
answers. If nothing answers, nothing is stored and you can try again.

To check again, for example after a firmware update, use **Reconfigure** on
the integration entry. Setup and Reconfigure show each step as it runs.
Devices added with an older version use the defaults until reconfigured.

## Command confirmation

Every command (Spray now, Spray: auto, Reset refill level) waits
up to 5 seconds for the device's acknowledgement. Without one, Home
Assistant shows an error, Spray: auto keeps its previous state, and the
status is read back so the entities show what the device is actually doing.

The channels in use and the battery voltage (both disabled by default) and the signal strength of the device's
advertisements (with the adapter or proxy that received them) are shown as
diagnostic sensors on the device page.

`6e400001-…` has no Client Characteristic Configuration descriptor, which
the ESPHome Bluetooth proxy normally requires before forwarding
notifications. When subscribing the normal way fails, the integration asks
the proxy to forward that characteristic directly.

Status replies are decoded as CBOR by key, so frames of other lengths (other
firmware string lengths, extra fields) work, as do frames split over several
notifications.

To save battery, the integration disconnects after each exchange, so the
link opening and closing is normal. **Connection status** shows whether the device is
actually working: it reads **OK**, and changes to **Unreachable** only if an
exchange fails or the device stops advertising. After a failed attempt the
integration retries after 30 s, then 1, 2 and 5 minutes, and every 5 minutes
from then on until the device answers. Its `last_update` attribute
shows when the last status arrived. The raw **Connection link** sensor
(Active/Idle) is disabled by default; enable it to debug connections.

## Troubleshooting

- **Device Not Found**: Ensure that your Godrej Aer Smart Matic device is powered on and within Bluetooth range. This devices seems to have bad connectivity range, so make sure that the device is pretty close to a Bluetooth proxy.
- **Connection Issues**: If you're using Bluetooth proxies, sometimes it seems that they "hang" on the connection. Restart them and try again.

## Development

The device library has a small test suite that runs without Home Assistant:

```
pip install pytest pytest-asyncio bleak bleak-retry-connector
pytest
```

The config flow, options flow and entry setup are tested against Home
Assistant itself, in a separate environment (Home Assistant's Bluetooth
component also needs its own requirements, listed in its `manifest.json`):

```
pip install pytest-homeassistant-custom-component
pytest tests_ha
```

## License

This project is licensed under the terms of the Apache 2.0 license.

## Contributions

Contributions are welcome! Please provide bug reports or suggestions via GitHub issues or submit a pull request for any enhancements or fixes.

## Disclaimer

This project is not associated with Godrej Aer. All rights regarding the devices are reserved to Godrej Aer.

## Warning

This integration is very experimental and is not officially supported. Use at your own risk.