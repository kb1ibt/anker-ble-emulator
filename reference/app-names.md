# The Anker app's names

The emulator has no upstream and nothing keys on its names, so it names its fields, values and commands after the Anker app, not after anker-solix-api or SolixBLE. This list is that standard: each app name, the wire field it reads, and the emulator name, which is the app name in snake_case. A device the app's logs don't cover takes the app's name for the same field on the closest equivalent hardware; a field the app has no name for anywhere gets a descriptive one.

**Rule.** Split the app's camelCase at each lower-to-upper step and before the last capital of a run of capitals, then lowercase: `workStatus` → `work_status`, `pd1OutputPower` → `pd1_output_power`, `TOUReservePower` → `tou_reserve_power`, `INVVersion` → `inv_version`, `supportPPSTOU` → `support_ppstou`. The app's own spellings are kept, typos included (`uploadtACPortTimedTaskInfo`). One name on several tags sets them all: `a4[23]` and `d9[3]` are both `chargingLimitValue`.

**Source.** The app's iOS log (`app_log`), app versions 3.13 to 3.23; from 3.24 the app encrypts its logs, so later sessions can't be read:

- **A1783 telemetry:** the `handleFetchDeviceInfoEvent {…}` property dump, matched against the decrypted `0421`/`c900` frame logged just before it (1 274 dump/frame pairs). *Agree* is the share of pairs where the app's value equals the field. Below 100 % the misses are dumps built from an earlier frame. A field the logs only ever show at one value is marked *constant*: its position comes from our docs, and the logs only confirm it. A percentage is given only for a field the 3 171 A1783 dumps show at more than one value, since one value agrees with any position.
- **A1783 commands:** `akiot.device.invoke_action` (the action id and its named parameters), then the `cmdID` and the payload it sends.
- **Prime (A2345, A91B2):** the handler the app runs for each received message (`---------> <name>`). The app logs no property dump for these.

The C Gen 2 models (A1763, A1765, A1783, A1785) run one display board, so the A1783 names hold for all four. Offsets count from after the type byte.

## A1783 telemetry (`0421`, `c900`)

When the app has several names for one field, the first is the emulator name: the one the app's own command uses, or else the most specific. The emulator sets a field by name only where its codec carries the field.

| field | app name | emulator name | agree |
|---|---|---|---|
| `a2[0]` | `protocolVersion` | `protocol_version` | 96 % (constant 32) |
| `a2[30]` | `voltageType` | `voltage_type` | constant 0 |
| `a3[0]` | `workStatus` | `work_status` | 99 % |
| `a3[1]` | `errorCode` | `error_code` | not re-checked (0, 25 and 40 seen) |
| `a3[2]` | `packageCount` | `package_count` | 100 % |
| `a3[3]` | `packageUseCount` | `package_use_count` | 100 % |
| `a3[4:6]` | `maxInputPower` | `max_input_power` | constant 1800 |
| `a3[6]` | `upgradeProgress` | `upgrade_progress` | 100 % |
| `a3[7]` | `wifiSignalStrength` (dBm = byte / 2 − 100) | `wifi_signal_strength` | 100 % |
| `a3[8:10]` | `mtuSize` | `mtu_size` | constant |
| `a3[10:12]` | `silentRechargePower` | `silent_recharge_power` | 100 % |
| `a3[12]` | `weakLightLockFlag` | `weak_light_lock_flag` | constant 0 |
| `a4[0:4]` | `acTimerValue` | `ac_timer_value` | 100 % |
| `a4[4:6]` | `acRechargePower` | `ac_recharge_power` | 99 % |
| `a4[6]` | `acFrequency` | `ac_frequency` | constant 60 |
| `a4[7]` | `acEnergySavingSwitch` | `ac_energy_saving_switch` | 100 % |
| `a4[8:12]` | `carChargerTimerValue` | `car_charger_timer_value` | 100 % |
| `a4[12]` | `carChargerEnergySavingSwitch` | `car_charger_energy_saving_switch` | 100 % |
| `a4[13:15]` | `deviceStandbyTime` | `device_standby_time` | 100 % |
| `a4[15:17]` | `screenOffTime` | `screen_off_time` | 100 % |
| `a4[17]` | `lcdBacklightBrightness` | `lcd_backlight_brightness` | 100 % |
| `a4[18]` | `ambientLightBrightness` | `ambient_light_brightness` | constant 0 |
| `a4[19]` | `temperatureUnit` | `temperature_unit` | constant 1 |
| `a4[20]` | `superFastChargeStatus` | `super_fast_charge_status` | constant |
| `a4[21]` | `screenSwitch` | `screen_switch` | 100 % |
| `a4[22]` | `memorySwitch` | `memory_switch` | 100 % |
| `a4[23]` | `chargingLimitValue` | `charging_limit_value` | 100 % |
| `a4[24]` | `dischargeLimitValue` | `discharge_limit_value` | 100 % |
| `a4[25]` | `deviceLanguage` | `device_language` | 100 % |
| `a4[26:28]` | `countryCode` (lead: blank on the A1783; the S2000 sends `US` there) | `country_code` | not checked |
| `a4[31]` bits 2-3 | `acInputDisableSwitch` | `ac_input_disable_switch` | 100 % |
| `a4[31]` bit 1 | `outageAlertSwitch` | `outage_alert_switch` | 100 % |
| `a5[0]` | `temperature` | `temperature` | 99 % |
| `a5[1]` | `chargeDischargeStatus` | `charge_discharge_status` | 100 % |
| `a5[2]` | `batteryLevel`, `battery`, `soc` | `battery_level` | 100 % |
| `a5[3]` | `health` | `health` | 100 % |
| `a6[0:2]` | `totalOutputPower` | `total_output_power` | 100 % |
| `a6[2:4]` | `acInputPower` | `ac_input_power` | 100 % |
| `a6[6:8]` | `remainingTime` | `remaining_time` | 100 % |
| `a7[0]` | `acOutputStatus` | `ac_output_status` | 100 % |
| `a7[1:3]` | `acOutputPower` | `ac_output_power` | 100 % |
| `a7[3]` | `acInputStatus` | `ac_input_status` | 100 % |
| `a8[0]` | `pv1ChargeStatus` | `pv1_charge_status` | 100 % |
| `a8[1:3]` | `pv1InputPower`, `carChargerInputPower` | `pv1_input_power` | 100 % |
| `aa[0]`, `aa[1:3]` | `pd1OutputStatus`, `pd1OutputPower` | `pd1_output_status`, `pd1_output_power` | 100 % |
| `ab[0]`, `ab[1:3]` | `pd2OutputStatus`, `pd2OutputPower` | `pd2_output_status`, `pd2_output_power` | 100 % |
| `ac[0]`, `ac[1:3]` | `pd3OutputStatus`, `pd3OutputPower` | `pd3_output_status`, `pd3_output_power` | 100 % |
| `ae[0]`, `ae[1:3]` | `usb1OutputStatus`, `usb1OutputPower` | `usb1_output_status`, `usb1_output_power` | 100 % |
| `b2[0]` | `carCharge1OutputStatus` | `car_charge1_output_status` | 100 % |
| `b2[1:3]` | `carCharge1OutputPower` | `car_charge1_output_power` | constant 0 (the socket never loaded); the position is the firmware's (`PiField/docs/re/a1783-telemetry.md` `b2`), and a C1000 Gen 2 was seen at `04 01 06 00`, 6 W (SolixBLE `tests/test_devices.py` `c1000g2_dc_on`) |
| `c0` | `subPackageInfo[]`: `subPackageSN`, `subPackageNumber`, `subPackageVersion`, `subPackageTemperature`, `subPackageStatus`, `subPackageBatteryLevel`, `subPackageHealth`, `subPackageBlinkStatus`, `subPackageSwitchStatus`, `subPackageErrorCode`, `subPackageConnectionStatus`, `subPackageBalanceStatus`, `subPackageType` | `sub_package_sn` … `sub_package_type` | field order |
| `ce` | `externalDeviceInfoList[]`: `externalDeviceProductCode`, `externalDeviceType`, `externalDeviceSn`, `externalDeviceVersion`; the mode and power have no app name | `external_device_product_code`, `external_device_sn`, `external_device_mode`, `external_device_output_power` | not checked |
| `d9[0]` | `TOUSystemStatus` | `tou_system_status` | constant 0 |
| `d9[1]` | `backupMode` | `backup_mode` | 99 % |
| `d9[2]` | `TOUReservePower` | `tou_reserve_power` | 100 % |
| `d9[3]`, `d9[4]` | `chargingLimitValue`, `dischargeLimitValue` (the `a4[23]`/`[24]` bytes) | as `a4` | 100 % |
| `d9[5]` | `TOUPeriods`, `peakValleyCount` | `tou_periods` (the count and its periods) | 100 % |
| `d9` tail `[0]` | `TOUSettingSystemStatus` | `tou_setting_system_status` | 99 % |
| `d9` tail `[1]` | `manualBackupSwitch` | `manual_backup_switch` | 100 % |
| `d9` tail `[2]` | `autoBackupSwitch` | `auto_backup_switch` | 100 % |
| `d9` tail `[3:7]`, `[7:11]` | `manualBackupStartTime`, `manualBackupEndTime` | `manual_backup_start_time`, `manual_backup_end_time` | 100 % |
| `d9` tail `[11:15]`, `[15:19]` | `backupStartTime`, `backupEndTime` | `backup_start_time`, `backup_end_time` | 100 % |
| `da[0]` bit 7 | `screenSaverSwitch` | `screen_saver_switch` | 100 % |
| `da[0]` bits 0-6 | `lcdTheme` | `lcd_theme` | 100 % |
| `da[1]` | `themeDownloadState` | `theme_download_state` | constant 0 |
| `da[2:6]` | `networkThemeID` | `network_theme_id` | constant 0 |
| `da[6:10]` | `networkThemeCRC` | `network_theme_crc` | constant 0 |
| `da[10]` | `lcdTimeFormat` | `lcd_time_format` | 100 % |
| `da[11:13]` | `lcdStartTime`, `dayStartTime` | `lcd_start_time` | constant 480 |
| `da[13:15]` | `lcdEndTime`, `dayEndTime` | `lcd_end_time` | 100 % |
| `da[15]` | `lcdRepeatCycle` | `lcd_repeat_cycle` | 100 % |
| `da[16]` | `dayNightSwitch` | `day_night_switch` | 100 % |
| `da[17]` | `dayBrightnessLevel` | `day_brightness_level` | 100 % |
| `da[18]` | `nightBrightnessLevel` | `night_brightness_level` | 100 % |
| `da[19:21]` | `nightStartTime` | `night_start_time` | 100 % |
| `da[21:23]` | `nightEndTime` | `night_end_time` | 100 % |
| `dc[0]` | `memorySwitchErrorCode` | `memory_switch_error_code` | not re-checked (0 and 3 seen) |
| `dc[1:5]` | `memorySwitchErrorTimestamp` | `memory_switch_error_timestamp` | not re-checked (0 and one epoch seen) |
| `f9[0:4]` | `mcuVersion`, `version`, `deviceVersion`; `packageVersion` is the same 4 bytes read big-endian | `mcu_version` | 100 % |
| `f9[4:8]` | `subMCUVersion` | `sub_mcu_version` | 100 % |
| `f9[8:12]` | `MPPTVersion` | `mppt_version` | constant v0.0.0.0 |
| `f9[12:16]` | `INVVersion` | `inv_version` | 100 % |
| `f9[16:20]` | `BMSVersion` | `bms_version` | 100 % |
| `f9[20:24]` | none | `sub_package_version` (descriptive) | — |
| `f9[24:28]` | `moduleVersion` | `module_version` | 100 % |
| `fa[0]` … `fa[4]` | `supportACEnergyMode`, `supportCarChargeMode`, `supportParallelUpgrade`, `supportGetUpgradeInfo`, `stationProtocolVersion` | `support_ac_energy_mode`, `support_car_charge_mode`, `support_parallel_upgrade`, `support_get_upgrade_info`, `station_protocol_version` | constant |
| `fa[5]` bits 7…0 | `supportSetCountryCode`, `supportChargingLimits`, `supportOutputStatus`, `supportScreenTimePeriod`, `supportUpgradeRecord`, `supportBackupDisaster`, `supportMemorySwitch`, `supportTransactionId` | `support_set_country_code` … `support_transaction_id` | constant |
| `fa[6]` bit 5 | none | `unknown_capability_6_5` (descriptive) | — |
| `fa[6]` bits 4…0 | `supportAcInputDisable`, `supportOutageAlert`, `supportSilentRecharge`, `memorySwitchTwo`, `supportPPSTOU` | `support_ac_input_disable`, `support_outage_alert`, `support_silent_recharge`, `memory_switch_two`, `support_ppstou` | 100 %; `supportOutageAlert` 89 % (the misses are all in one firmware-update launch) |
| `fd` | `transactionId` | `transaction_id` | 100 % |
| `fe` | `timestamp` | `timestamp` | 100 % |

The app also carries names it builds itself, not from a field: `isOTA`, `otaEnable`, `maxBatteryNum`, `deviceCategory`, `balancedStatus`, `accessoryInfo`, and the identity keys `productCode`, `pnCode`, `devicePnCode`, `deviceSn`, `deviceSN` (the PN and SN in `a2`). The `0489` OTA push's `a2` takes `ota_enable` from `otaEnable`; its `a3` block reason and the `0425` event report have no app name and keep descriptive ones (`ota_block_reason`, `event_code`, `event_start_timestamp`, `event_end_timestamp`, `event_soc`).

## A1783 commands

The function name is the action id without its `action_` prefix. A parameter keeps the app's name for it, which isn't always the telemetry name of the field it sets.

| action | cmd | parameters → tag | emulator name |
|---|---|---|---|
| `action_set_ac_params` | `0101` | `acInputStatus` → `a2` (the AC output switch), `acTimerValue` → `a3`, `acRechargePower` → `a4`, `acEnergySavingSwitch` → `a6` | `set_ac_params` |
| `action_set_car_charger_params` | `0102` | `carChargerInputStatus` → `a2` (the DC/car output switch), `carChargerTimerValue` → `a3` | `set_car_charger_params` |
| `action_set_system_params` | `0103` | `screenSwitch` → `a2`, `lcdBacklightBrightness` → `a3`, `screenOffTime` → `a4`, `deviceStandbyTime` → `a6`, `deviceLanguage` → `a9`, `chargingLimitValue` → `aa`, `dischargeLimitValue` → `ab` | `set_system_params` |
| `action_set_bms_params` | `0104` | `subPackageNumber` → `a2`, `subPackageSN` → `a3`, `subPackageSwitchStatus` → `a4`, `subPackageBlinkStatus` → `a5` | `set_bms_params` |
| `action_set_screen_saver_params` | `0091` | `screenSaverSwitch` + `lcdTheme` → `a2`, `lcdTimeFormat` → `a7`, `lcdStartTime` → `a8`, `lcdEndTime` → `a9`, `lcdRepeatCycle` → `aa`, `dayNightSwitch` → `ab`, `dayBrightnessLevel` → `ac`, `nightBrightnessLevel` → `ad`, `nightStartTime` → `ae`, `nightEndTime` → `af` | `set_screen_saver_params` |
| `action_set_backup_mode` | `005e` | `backupMode`, `manualBackupSwitch`, `autoBackupSwitch` | `set_backup_mode` |
| `action_set_tou_system_params` (from anker-solix-api-exploration; in our logs only encrypted) | `0090` | — | `set_tou_system_params` |

## Prime (A2345, A91B2) messages

The app names each message by the handler it runs; a reply's handler ends in `Res`. A setter's function name is its reply handler without `Res`.

| msgtype | app handler | emulator name | devices |
|---|---|---|---|
| `0302` | `uploadACPortSwitchState` | `upload_ac_port_switch_state` | both |
| `0303` | `uploadDCPortState` | `upload_dc_port_state` | both |
| `0305` | `uploadEasterEggTrigger` | `upload_easter_egg_trigger` | both |
| `0307` | `uploadACPortCountdownInfo` | `upload_ac_port_countdown_info` | both |
| `0308` | `uploadtACPortTimedTaskInfo` | `uploadt_ac_port_timed_task_info` | both |
| `030b` | `uploadScreensaver` | `upload_screensaver` | A2345 |
| `030d` | `uploadChargingMode` | `upload_charging_mode` | A2345 |
| `030f` | `uploadPortPriority` | `upload_port_priority` | A2345 |
| `0311` | `uploadThemeDownloadSuccess` | `upload_theme_download_success` | both |
| `0830` | `getZXOTAStateRes` | `get_zxota_state` | both |
| `0a00` | `getAllDeviceInfoRes` | `get_all_device_info` | both |
| `0a01` | `setResetRes` | `set_reset` | A91B2 |
| `0a02` | `getScreensaverRes` | `get_screensaver` | A2345 |
| `0a03` | `setScreenTimeoutRes` | `set_screen_timeout` | both |
| `0a04` | `setScreenBrightnessRes` | `set_screen_brightness` | both |
| `0a05` | `setScreensaverRes` | `set_screensaver` | both |
| `0a06` | `setChargingModeRes` | `set_charging_mode` | both |
| `0a07` | `setDCRelaySwitchRes`, `setACRelaySwitchRes` (by the port switched) | `set_dc_relay_switch`; the A91B2's, which switches either kind, `set_relay_switch` | both |
| `0a08` | `setACPortTimedTaskRes` | `set_ac_port_timed_task` | both |
| `0a09` | `setACPortCountdownTaskRes` | `set_ac_port_countdown_task` | both |
| `0a0a` | `setBindSuccessRes` | `set_bind_success` | both |
| `0a0c` | `setPortPriorityRes` | `set_port_priority` | A2345 |
| `0a0e` | `setButtonRotateDirection` | `set_button_rotate_direction` | A2345 |
| `0a0f` | `setHolidayScreensaverRes` | `set_holiday_screensaver` | A2345 |
| `0a10` | `setTimeFormatRes` | `set_time_format` | both |
| `0a11` | `setCustomChargeModeSwitchRes` | `set_custom_charge_mode_switch` | both |
| `0a20` | `prepareSendScreensaverRes` | `prepare_send_screensaver` | both |
| `0a21` | `sendScreensaverDataRes` | `send_screensaver_data` | both |
| `0a23` | `setEquipmentIdentificationSwitchRes` | `set_equipment_identification_switch` | A2345 |

The A91B2's `4214` is the app's `setAcLightMode` (`ac_light_mode`, `b5`). The A2345's `4213` (screensaver window) and `4214` (compatibility switch) have no app method name: `set_screensaver_window`, `set_compatibility_switch`.

## Alternator Charger (AS200) settings

Our logs have no AS200. Its settings take the parameter names of the app's AS200 `action_set_system_params` from [moag1000/anker-solix-api-exploration](https://github.com/moag1000/anker-solix-api-exploration) `VALIDATION_MODEL.md` (unverified against a log), matched to the `0103` tag each setting sends in the [anker-solix-api#290](https://github.com/thomluther/anker-solix-api/discussions/290) traces and to the `a4` byte that follows it.

| `0103` tag | `0421` field | app name | emulator name |
|---|---|---|---|
| `ac` | `a4[0]` | `powerSwitch` | `power_switch` |
| `a2` | `a4[1]` | `currentWorkMode` (0 Charging Mode, 1 Reverse Mode) | `current_work_mode` |
| `a3` | `a4[2]` | `batteryType` (0 LiFePO4, 1 lead-acid) | `battery_type` |
| `aa` | `a4[3]` | `batteryVoltageType` (0 12 V, 1 24 V) | `battery_voltage_type` |
| `b4` | `a4[5:7]` | `powerOutVoltage` (0.1 V) | `power_out_voltage` |
| `a5` | `a4[7:9]` | `chargingModePower` | `charging_mode_power` |
| `a6` | `a4[9:11]` | `reverseModePower` | `reverse_mode_power` |
| `ae` | `a4[11:13]` | `shutdownTimeout`, assumed: the timeout in force, 0 when never | `shutdown_timeout` |
| `b2` | `a4[13]` | `temperatureUnit` | `temperature_unit` |
| `bb` | `a4[16:18]` | `shutdownTimeoutV2`, assumed: the slider's value | `shutdown_timeout_v2` |
| `bc` | `a4[18]` | `neverShutdown` | `never_shutdown` |

`b8` (power off; only the button brings it back) sets no field. `shutdownStatus`, `powerUnlock` and `lowLoadAutoShutdownTimeout` have no tag in the traces.

## Names by equivalence

Where the logs give no name for a field, it takes the app's name for the same thing on the closest logged hardware.

| device | field | emulator name | from |
|---|---|---|---|
| A2345, A91B2 | USB-C ports 1-4, USB-A ports 1-2 | `pd1`-`pd4`, `usb1`-`usb2` (the A2345's shared USB-A record: `usb`; the A91B2's AC outlets: `ac1`, `ac2`) | A1783 `pd1…`, `usb1…` |
| A2345, A91B2 | a port's status, voltage, current, power | `<port>_output_status`, `_output_voltage`, `_output_current`, `_output_power` | A1783 `pd1OutputStatus`, `pd1OutputPower` |
| A2345, A91B2 | a port's switch; its on/off schedule; its countdown; its priority | `<port>_relay_switch`; `_timed_task_start_*`, `_timed_task_end_*`; `_countdown_task_switch`, `_seconds`, `_remaining_seconds`; `_port_priority` | Prime `setDCRelaySwitch`, `setACPortTimedTask`, `setACPortCountdownTask`, `setPortPriority` |
| A2345, A91B2 | screen timeout, brightness, clock format | `screen_timeout`, `screen_brightness`, `time_format` | Prime `setScreenTimeout`, `setScreenBrightness`, `setTimeFormat` |
| A2345, A91B2 | clock screensaver switch, theme, theme id; holiday switch | `screen_saver_switch`, `screen_saver_theme`, `screen_saver_theme_id`; `holiday_screensaver_switch` | A1783 `screenSaverSwitch`; Prime `setHolidayScreensaver` |
| A2345 | knob direction, equipment identification | `button_rotate_direction`, `equipment_identification_switch` | Prime `setButtonRotateDirection`, `setEquipmentIdentificationSwitch` |
| A91B2 | OTA busy, OTA progress | `is_ota`, `upgrade_progress` | A1783 `isOTA`, `upgradeProgress` |
| S2000 | `a6[0:13]` | `total_output_power`, `ac_input_power`, `pv1_input_power`, `remaining_time`, `battery_level`, `ac_input_plug_status` (descriptive), `total_input_power` | A1783 |
| S2000 | `f0[0]` (AC charger plugged in) | `ac_input_status` | A1783 `acInputStatus` |
| S2000 | `aa` (one USB total) | `pd1_output_status`, `pd1_output_power` | A1783 tag `aa` |
| A17C0 | battery, temperature unit, solar input, output, flow state, firmware, controller firmware, discharge cutoff | `battery_level`, `temperature_unit`, `pv1_input_power`, `total_output_power`, `charge_discharge_status`, `device_version`, `sub_mcu_version`, `discharge_limit_value` | A1783 |
| F2000 | AC output, the two DC outputs, power saving, serial | `ac_output_status`, `car_charge1_output_status`, `car_charge2_output_status`, `ac_energy_saving_switch`, `device_sn` | A1783 |
| C Gen 2 `0490` | battery, expansion battery, output, input, DC input, remaining time, expansion entries, Storm Guard counters | `battery_level`, `sub_package_battery_level`, `total_output_power`, `total_input_power`, `pv1_input_power`, `remaining_time`, `sub_package_*`, `auto_backup_*` | A1783 `0421` |
| C Gen 2 `0490` | `.24` time and change counts of the four tracked settings | `lcd_backlight_brightness_*`, `ac_recharge_power_*`, `super_fast_charge_status_*`, `memory_switch_*` | A1783 `0421` |
| AS200 | `a6[0]` and the attached station's `a7[3]` (0 idle, 1 the station discharges into the car, 2 it charges) | `work_status`, `external_device_work_status` | A1783 `workStatus` |
| AS200 | `a3[4:6]`, repeated at `a6[25:27]`: the power moved | `total_output_power` | A1783 `totalOutputPower` |
| AS200 | `a6[2]`, `a7[2]` | `temperature`, `external_device_temperature` | A1783 `temperature` |
| AS200 | `a7`: the station on the expansion cable, its serial, PN and SoC | `external_device_sn`, `external_device_product_code`, `external_device_battery_level` | A1783 `externalDeviceSn`, `externalDeviceProductCode`, `batteryLevel` |
| AS200 | `a7[4:6]`, `a7[6:8]`: the station's time to full and to empty (0.1 h) | `external_device_remaining_charge_time`, `external_device_remaining_discharge_time` | A1783 `remainingTime`, one field there |
| AS200 | `f9`, `0489` | `mcu_version`, `module_version`; `ota_enable`, `ota_block_reason` | A1783 |

Fields with no equivalent keep descriptive names: the A2345's screensaver window, custom charge profile (`b8`, `custom_charge_profile`) and the `4a02` `a6` switch `4222` sets (`port_widgets_switch`), the A91B2's charging submode, the A17C0's battery charge power, energies, cutoffs, inverter, hardware version and 0W output switch, the F2000's light bar, the AS200's slider limits (`charging_mode_power_min` … `power_out_voltage_max`), car battery voltage (`battery_voltage`) and cable states (`output_cable_status`, `xt60i_cable_status`), and the rest of the `0490`. The `0490`'s descriptive names follow `PiField/docs/re/firmware/c490-schema.md`: `.15.8` / `.15.9` are the warning and fault bitmaps of one protection set (`bms_warning_bits`, `bms_fault_bits`); `.15.10`–`.15.12`, whose ids aren't all pinned, are named by the severity tables they repack (`bms_status_bits_ac`, `_c`, `_b`); `.15.7`, never written, is named by its pack-block offset (`bms_field_2c`); and `.14.28`, which nothing fills, is `pack_reserved`. A `0490` integer that the `0421` also carries as bytes ends in `_code` (`mcu_version_code`, `bms_version_code`). The names carry no unit, since a value is the raw count the frame holds (`pack_current` in 10 mA, `cumulative_charge` in 0.1 Ah, `ac_in_energy` in 0.9 Wh); only `counter_page_hours`, whose count is whole hours, keeps one.

## Compared with anker-solix-api-exploration

[moag1000/anker-solix-api-exploration](https://github.com/moag1000/anker-solix-api-exploration) (an AI-assisted decompile of app v3.18.0) has 32 of the 171 logged names above verbatim; its A2345 setter section cites our own bench captures, so its agreement there isn't independent. Unverified against any log, it adds:

- **App actions for devices not in our logs:** `action_set_ac_params`, `action_set_bms_params`, `action_set_car_charger_params` on the A1782; `action_set_system_params` on the AS200 (`temperatureUnit`, `shutdownStatus`, `shutdownTimeout`, `powerSwitch`, `currentWorkMode`, …); for the A2345/A2687 screen `action_set_screen_off_time`, `action_set_screen_orientation`, `action_set_lcd_backlight_brightness`; on the A2687 `action_set_charging_protocol`, `action_set_dc_port_switch`, `action_set_dc_port_countdown`, `action_set_custom_mode_switch_type`, `action_set_gyroscope_switch`; on the A25X7 `action_set_sleep_mode`.
- **Actions that may be the A1783's other setters:** `action_set_tou_system_params` (for `4090`) and `action_set_cycle_charging_power`. The app logs them, but only in the encrypted logs it writes from 3.24 on, which can't be read here.
- **Prime client methods** (the app's sending side; the logs above show the reply handlers): `setDCPortCountdownTask`, `setScreenSaverSwitchAndTheme` (`4205`), `setClockFormat` (A91B2), `setUFCSSwitch`; and the identifiers `UFCSSwitchValue`, `otaModuleVersion`, `acPortSwitchState`, `acPortNumber`.

Its A2345 status-tag names are its own translations of the app's Chinese debug strings, not app identifiers, and several don't fit the recorded snapshot (per-port info at `a4`-`a6`, an AC switch at `bc` on a charger with no AC outlet). Two of its meanings conflict with the logs: `remainingTime` is the battery's `a6[6:8]` (0.1 h to full or empty, 100 % agreement), not seconds left in a backup window; and the app sends `backupMode=2`, outside its 0 / 1 / 4 enum.
