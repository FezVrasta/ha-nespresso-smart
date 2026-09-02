# Nespresso Vertuo BLE protocol

Reverse-engineered from `Nespresso Smart Machines` Android app v1.3.0
(`com.nestle.us.nespresso.nespressosmartmachines`), package
`com.sdataway.vertuonext.*` (the vendor BLE SDK) and
`com.nestle.us.nespresso.iot.*` (the app layer that drives it).

Everything below is quoted from decompiled code; the source class is named for
each item so it can be re-checked.

---

## 1. Which SDK drives a Vertuo Pop

`domain/model/MachineTypeKt` maps marketing names to model-number prefixes:

| MachineType       | Model prefixes (BLE name)      | Codename        |
|-------------------|--------------------------------|-----------------|
| VertuoNext        | `CV1` `DV1` `CV3` `DV3`        | `VENUS`         |
| **VertuoPop**     | **`CV2` `DV2`**                | **`VENUS_ONE`** |
| VertuoLattissima  | `DV5`                          | `VENUS_1PLUS1_DL` |
| VertuoCreatista   | `CV5`                          | `VENUS_1PLUS1_BR` |
| VertuoPopPlus     | `CV6` `DV6`                    | `VENUS_MOON`    |
| VertuoUp          | `MC1` `MD1` `MC2` `MD2`        | `VENUS_MINI`    |
| Barista           | `W10` `W11`                    | `WHITE`         |

`MachineTypeKt.e()` routes everything except VertuoUp and Barista to
`IoTMachineType.VertuoNextMachine`. **A Vertuo Pop is therefore driven by the
`vertuonext` SDK**, and uses the `06AA…` GATT profile below.

(`VertuoUp` uses a completely different profile: `96600100-…` / `E0F00100-…`.
`Barista` uses `6524…`, which is the `06AA…` profile with a different base UUID.)

BLE scan filter (`IoTDataSourceVertuoNextImpl.maskPrefixes`):
`CV1 DV1 CV3 DV3 CV2 DV2 DV5 CV5 CV6 DV6 W10 W11 Venus`

---

## 2. GATT profile

From each `Charac*.toString()` in `com.sdataway.vertuonext.sdk.characteristics`,
which returns `"<service>:<characteristic>"`.

Service `06AA1910-F22A-11E3-9DAA-0002A5D5C51B` (identity / pairing)

| Characteristic                         | Name                  | Access |
|----------------------------------------|-----------------------|--------|
| `06AA3A11-F22A-11E3-9DAA-0002A5D5C51B` | ProfileVersion        | read   |
| `06AA3A21-F22A-11E3-9DAA-0002A5D5C51B` | MachineInfo           | read   |
| `06AA3A31-F22A-11E3-9DAA-0002A5D5C51B` | SerialNumber          | read   |
| `06AA3A41-F22A-11E3-9DAA-0002A5D5C51B` | **CMID** (auth key)   | write  |
| `06AA3A51-F22A-11E3-9DAA-0002A5D5C51B` | CMIDType (pair state) | read/notify |
| `06AA3A61-F22A-11E3-9DAA-0002A5D5C51B` | TXLevelChangeRequest  | write  |

Service `06AA1920-F22A-11E3-9DAA-0002A5D5C51B` (runtime state)

| Characteristic                         | Name                  | Access |
|----------------------------------------|-----------------------|--------|
| `06AA3A12-F22A-11E3-9DAA-0002A5D5C51B` | **MachineStatus**     | read/notify |
| `06AA3A22-F22A-11E3-9DAA-0002A5D5C51B` | MachineSpecificParams | read   |
| `06AA3A42-F22A-11E3-9DAA-0002A5D5C51B` | CommandReq            | write  |
| `06AA3A52-F22A-11E3-9DAA-0002A5D5C51B` | CommandRsp            | notify |

Service `06AA1930-F22A-11E3-9DAA-0002A5D5C51B` (errors)

| `06AA3A13-…` ErrorSelection (write) · `06AA3A23-…` ErrorInformation (read) |

Service `06AA1940-F22A-11E3-9DAA-0002A5D5C51B` (settings)

| `06AA3A44-…` GeneralUserSettings (read/write) |

Service `06AA1990-F22A-11E3-9DAA-0002A5D5C51B` (Wi-Fi onboarding)

| `06AA3A19-…` WifiSetup (w) · `06AA3A29-…` WifiCurrentSetup (r/n) ·
  `06AA3A39-…` WifiScanSelection (w) · `06AA3A49-…` WifiScanResult (r/n) ·
  `06AA3A79-…` IotMarketName (r/w) |

---

## 3. Pairing / authentication

Fully client-side, with **no cloud call involved**. From
`IoTDataSourceVertuoNextImpl.getSeed()` / `getSecretToPass()` and
`PairingUtils`.

### 3.1 Generate the seed (once per machine, then stored forever)

`PairingUtils.generatePairingKey()`:

```
seed = hex(int.from_bytes(sha1(str(uuid4()).encode()).digest()))[:32]   # 32 hex chars
```

(Java: `new BigInteger(1, sha1(uuid)).toString(16).substring(0, 32)`, an
unsigned big-endian integer rendered in base 16, so leading zero nibbles are
dropped before truncation.)

The seed is the value the app persists and syncs to the Nespresso account. It is
the only thing you need to back up, since the on-air secret is derived from it.

### 3.2 Derive the 8-byte secret written to the CMID characteristic

`PairingUtils.prepareHashForPairing()` then `getBufferFromByteArray()`:

```
h  = bytes.fromhex((seed + "0")[:16])       # first 16 hex chars -> 8 bytes
out[0] = ((h[0] & 0xF0) >> 4) | 0x80
out[i] = ((h[i-1] & 0x0F) << 4) | ((h[i] & 0xF0) >> 4)   for i in 1..7
```

Written out as nibbles this is simply:

```
secret_hex = "8" + first 15 nibbles of h
```

i.e. **the secret is always 8 bytes / 16 hex chars and always starts with `8`.**
That matches the observation in `fsalomon/nespresso-expert-ble` that the sniffed
auth value is "a 16 character hex string that starts with 8". The Original line
(Expert/Prodigio) and the Vertuo line share this scheme.

### 3.3 Onboarding sequence (machine not yet bound)

1. Connect, and perform a BLE (SMP) bond. CMID is a protected characteristic:
   until the link is encrypted, BlueZ answers the write with
   `[org.bluez.Error.NotPermitted] Not paired`. (CMIDType at step 2 reads
   fine unbonded.)

   Do not *block* on the bond, though. BlueZ's `Pair()` is a D-Bus call with
   no deadline of its own and never returns when the machine ignores the SMP
   request or no pairing agent is registered. It is also frequently
   unnecessary to wait for: the refused write is itself what prompts the
   stack to bond, so a first attempt that fails `NotPermitted` and a second
   that succeeds is the normal path on Home Assistant OS. Bound the bond,
   press on regardless, and retry the connection.
2. Read CMIDType `06AA3A51`. Value `0`/`1` = not bound, `2` = bound (`FINAL`),
   `3` = undefined. (`MachineStatus.PairingKeyState`.)
3. If not bound:
   - write `0x01` to TXLevelChangeRequest `06AA3A61`
   - write the 8-byte secret to CMID `06AA3A41`
4. Re-read CMIDType to confirm it became `2`.

On every later connection, just write the same secret to CMID `06AA3A41`
before touching any protected characteristic.

A machine already bound to another key must be factory-reset on the machine
itself before it will accept a new one (app error `MachineAlreadyPairedWithOtherAccount`).

---

## 4. MachineStatus (`06AA3A12`, read + notify)

From `com.sdataway.vertuonext.sdk.models.MachineStatus(byte[])`. Payload is
zero-padded to 8 bytes; only the first 3 are decoded.

Byte 0:

| Bit | Field |
|-----|-------|
| 7   | `bootloaderActive` |
| 6–5 | `pairingKeyState` (0 NONE, 1 TEMPORARY, 2 FINAL, 3 UNDEFINED) |
| 4   | `errorPresent` |
| 3   | `ledSignalingActive` |
| 2   | `descalingNeeded` |
| 1   | `cleaningNeeded` |
| 0   | `waterTankEmpty` |

Byte 1:

| Bit | Field |
|-----|-------|
| 7   | `brewingUnitClosed` |
| 6   | `capsuleContainerFull` |
| 5   | `manualProgCupLengthInProgress` |
| 4   | `milkFrotherRunning` |
| 3–0 | low nibble of machine state |

Machine state = `(byte1 & 0x0F) + (byte2 & 0xF0)`.

| Val | State | Val | State |
|-----|-------|-----|-------|
| 0  | FACTORY_RESET   | 14 | RINSING |
| 1  | HEATUP          | 17 | CAPSULE_READING |
| 2  | READY           | 18 | DESCALE_SEQUENCE_DECODING |
| 3  | DESCALING_READY | 19 | TANK_EMPTY |
| 4  | BREWING         | 20 | DESCALING_PAUSED |
| 5  | CLEANING        | 21 | INITIALIZATION |
| 6  | DESCALING       | 22 | RINSING_READY |
| 7  | EMPTYING        | 23 | MAINTENANCE_MENU |
| 8  | DEVICE_ERROR    | 26 | CLEANING_PAUSED |
| 9  | POWER_SAVE      | 33 | EMPTYING_READY |
| 10 | COOLDOWN        | 34 | CLEANING_READY |
| 11 | SERVICE_MODE    | 35 | READY_OLD_CAPSULE |
| 12 | STANDBY         | 36 | RINSING_PAUSED |
| 13 | UPDATING        | 255| UNKNOWN |

---

## 5. MachineInfo (`06AA3A21`, read, 16 bytes)

From `CharacMachineInfo`. All uint16 are **big-endian**
(`ByteBufferManager.b`). Versions render as `value/100 . value%100`
(`Utils.b`).

| Offset | Field |
|--------|-------|
| 0–1  | hardwareVersion |
| 2–3  | bootloaderVersion |
| 4–5  | firmwareVersion |
| 6–7  | recipeDatabaseVersion |
| 8–9  | connectivityFirmwareVersion → `v/10000 . (v%10000)/100 . v%100` |
| 10–15| deviceAddress (6-byte MAC) |

Note this differs from the Original-line layout used by
`bulldog5046/ha_nespresso_integration`, which has no `recipeDatabaseVersion`
field and so mis-reads everything from offset 6 onward.

---

## 6. GeneralUserSettings (`06AA3A44`, read/write, 4 bytes)

From `CharacGeneralUserSettings`.

| Offset | Field |
|--------|-------|
| 0-1 | `machineAPOTime` (uint16 **little**-endian, `ByteBufferManager.a`) |
| 2   | `waterHardness` |
| 3   | `activeTime2StandBy` |

Read-modify-write is the safe way to change hardness.

---

## 7. What is NOT in the protocol: brewing

The entire public surface of `VertuoNextMachine` is:

```
pair                    getMachineStatus         getWiFiNetworks
setPairingKey           getFirmwareVersion       getWiFiSettings
getPairingKeyState      getWaterHardnessLevel    setWiFiSettings
performFactoryReset     setWaterHardnessLevel    setMarketID
performPostPairingSteps getLastDisconnectionErrorCode
checkMachineIsConnected  disconnect              registerNotificationCallback
```

There is **no brew / start-preparation / cup-size command anywhere** in the
Vertuo SDK or in the app that drives it. Grepping the whole APK for
`startBrew|brewCommand|startPreparation|launchBrew|makeCoffee|remoteBrew`
returns nothing.

This is a hardware design decision, not an omission: a Vertuo reads a barcode
on the capsule rim to pick its own brew programme, and the physical lever must
be cycled for each cup. The official app cannot brew remotely either.

The `CommandReq` `06AA3A42` characteristic does exist (format: `cmdID:u16`,
`subCmdID:u16`, `dataControl`, `data[]`, see `CCommandReq`), and on the
**Original** line the same characteristic accepts
`03 05 07 04 00 00 00 00 <temp> <brewtype>`. No Vertuo code path ever writes to
it, and the Vertuo firmware's accepted opcodes are unknown.

**Conclusion: a Vertuo Pop integration is read-only telemetry plus settings.**

---

## 8. Recovering an existing pairing key from the account API

The pairing key is stored server-side, so a machine already bound to the phone
app does not need a factory reset. From `EcapiMachinesEndpoints.GetUserMachines`
(`path()` at line ~2298 builds `ecapi/machines/v1/{country}/{channel}/{ownerId}`)
and `EcapiNcsAuthEndpoints`:

| Call | Path |
|---|---|
| account | `GET /ecapi/identityprovider/v1/web-accounts/me` → `{email, market, …}` |
| customer | `GET /ecapi/customers/v7/{market}/b2c/me` → `{memberNumber, …}` |
| machines | `GET /ecapi/machines/v1/{market}/b2c/{memberNumber}` |

`Channel` resolves to `b2c` (`com/nestle/nespresso/util/Channel.java`), and
`memberNumber` is the `ownerId`.

The machines response (`UserMachineResponse`) contains, per machine (values here
are placeholders, not from a real account):

```json
{
  "type": "machinesVenusOneProfile",
  "serialNumber": "12345DV2a09876543Zz",
  "macAddress": "AA:BB:CC:DD:EE:FF",
  "pairingKey": "A1B2C3D4E5F60718293A4B5C6D7E8F90",
  "secret": "ihssPU5fYHE=",
  "machineSerialized": "VENUS_ONE||DV2_AABBCCDDEEFF|…"
}
```

**`secret` is `base64(derive_secret(pairingKey))`**, so the server independently
confirms the §3.2 derivation. The placeholder above is internally consistent, so
you can check it yourself:

```python
>>> base64.b64encode(derive_secret("A1B2C3D4E5F60718293A4B5C6D7E8F90".lower()))
b'ihssPU5fYHE='
```

This was confirmed against a real Vertuo Pop: the `secret` the server returned
matched `derive_secret(pairingKey)` byte for byte, and the `serialNumber` and
`macAddress` in the response matched what `probe.py` read from the machine over
BLE. Those real values are not reproduced here.

### Why this must be done in a browser

Every request to these endpoints passes through Akamai Bot Manager. The app
ships `libakamaibmp.so` and `com.cyberfend.cyfsecurity.SensorDataBuilder`, and
`AkamaiInterceptor` attaches the result as an `X-acf-sensor-data` header:

```java
// com/nestle/nespresso/idp/interceptor/AkamaiInterceptor.java
builderB.a("X-acf-sensor-data", CYFMonitor.g());
```

That payload encodes device telemetry and cannot be produced off-device, so a
scripted HTTP client gets `403 {"errorReason":"NOT_ALLOWED","code":"CLIENT_REQUEST"}`.
A rooted emulator fails the same check (`ro.kernel.qemu=1`, `dev-keys`,
`ro.debuggable=1`, stale Play Services), even running the genuine SDK with no
proxy in the path.

A logged-in browser already holds valid Akamai cookies (`_abck`, `bm_sz`,
`bm_sv`), so the same calls succeed from the DevTools console. See the README
for the snippet.

Incidental finding: the Android app logs its `Authorization: Basic` header (so
the account email and password in cleartext) to logcat at INFO level via an
OkHttp logging interceptor left enabled in the release build.
