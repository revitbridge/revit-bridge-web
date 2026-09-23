/* The device routes of the v1 bridge (spec 7, section 3).

   A device is driven by its own browser key, which is not always the selected
   one: pairing polls the device it just created, and the list shows every
   device this browser holds a key for. So each call carries the key it is
   about, instead of relying on the session's headers. */

import type { DeviceStatus, DevicesStatus, PairingResponse } from '../../types/api'
import { del, get, post } from '../shared/http'

const B = '/api/v1/bridge'
const enc = encodeURIComponent

export const keyHeaders = (deviceId: string, browserKey: string) =>
  ({ 'X-Device-Id': deviceId, 'X-Device-Key': browserKey })

export const deviceApi = {
  /* A new device, its one-time pairing code and this browser's key for it. */
  pair: (label: string) => post<PairingResponse>(`${B}/devices/pair`, { label }),

  /* What the relay knows about one device; 404 unknown_device, 403 invalid_device_key. */
  status: (deviceId: string, browserKey: string) =>
    get<DeviceStatus>(`${B}/devices/${enc(deviceId)}`, keyHeaders(deviceId, browserKey)),

  /* Revoking closes the add-in's connection and makes both the key and the device token useless. */
  revoke: (deviceId: string, browserKey: string) =>
    del<{ status?: string }>(`${B}/devices/${enc(deviceId)}`, keyHeaders(deviceId, browserKey)),

  /* Admin only (X-Admin-Token, sent by the shared request helper): every device this host knows. */
  all: async () => (await get<{ devices: DeviceStatus[] }>(`${B}/devices`)).devices,

  /* How many devices may connect at once, and how many are connected. */
  capacity: () => get<DevicesStatus>(`${B}/slots`),
}

export type DeviceApi = typeof deviceApi
