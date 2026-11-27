/* Drawing a rough element shape on a 2D canvas: no library, a few hundred triangles at most. */

const TILT = 0.45              // the camera looks down by this much (radians)
const FLAT = 0.999             // two triangles whose normals agree this much are one face

export interface Mesh { vertices: number[]; triangles: number[] }

/* The corners and faces of a box of the given size, as a mesh. */
export function boxMesh(size: number[]): Mesh {
  const [x, y, z] = size.map(v => Math.max(v, 1))
  const vertices = [0, 0, 0, x, 0, 0, x, y, 0, 0, y, 0, 0, 0, z, x, 0, z, x, y, z, 0, y, z]
  const quads = [[0, 3, 2, 1], [4, 5, 6, 7], [0, 1, 5, 4], [1, 2, 6, 5], [2, 3, 7, 6], [3, 0, 4, 7]]
  return { vertices, triangles: quads.flatMap(([a, b, c, d]) => [a, b, c, a, c, d]) }
}

export const sizeText = (size: number[]) => size.map(v => Math.round(v)).join(' × ') + ' mm'

/* Which sides of each triangle are the outline of a face: one flag per side, in the order
   (a-b, b-c, c-a). A side two triangles share while lying in one plane is inside a face and
   is not drawn, so a wall's side reads as one surface instead of its triangles. Corners are
   matched by position: the triangles of different faces come with their own corner numbers. */
export function featureEdges(mesh: Mesh): Uint8Array {
  const { vertices, triangles } = mesh
  const count = triangles.length / 3
  const flags = new Uint8Array(count * 3)
  const corner = new Map<string, number>()
  const welded = new Int32Array(vertices.length / 3)
  for (let i = 0; i < welded.length; i++) {
    const key = vertices[i * 3] + ',' + vertices[i * 3 + 1] + ',' + vertices[i * 3 + 2]
    let index = corner.get(key)
    if (index === undefined) { index = corner.size; corner.set(key, index) }
    welded[i] = index
  }
  const normals = new Float64Array(count * 3)
  for (let t = 0; t < count; t++) {
    const a = triangles[t * 3] * 3, b = triangles[t * 3 + 1] * 3, c = triangles[t * 3 + 2] * 3
    const ux = vertices[b] - vertices[a], uy = vertices[b + 1] - vertices[a + 1], uz = vertices[b + 2] - vertices[a + 2]
    const vx = vertices[c] - vertices[a], vy = vertices[c + 1] - vertices[a + 1], vz = vertices[c + 2] - vertices[a + 2]
    const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx
    const length = Math.hypot(nx, ny, nz) || 1
    normals[t * 3] = nx / length; normals[t * 3 + 1] = ny / length; normals[t * 3 + 2] = nz / length
  }
  const sides = new Map<string, number[]>()          // welded corner pair -> the sides on it
  for (let t = 0; t < count; t++) {
    for (let s = 0; s < 3; s++) {
      const p = welded[triangles[t * 3 + s]], q = welded[triangles[t * 3 + (s + 1) % 3]]
      if (p === q) continue                             // a triangle without area has no outline
      const key = p < q ? p + ':' + q : q + ':' + p
      const list = sides.get(key)
      if (list) list.push(t * 3 + s)
      else sides.set(key, [t * 3 + s])
    }
  }
  for (const list of sides.values()) {
    let inside = list.length === 2
    if (inside) {
      const first = Math.floor(list[0] / 3) * 3, second = Math.floor(list[1] / 3) * 3
      const dot = normals[first] * normals[second] + normals[first + 1] * normals[second + 1] + normals[first + 2] * normals[second + 2]
      inside = Math.abs(dot) >= FLAT
    }
    if (!inside) for (const side of list) flags[side] = 1
  }
  return flags
}

const outlines = new WeakMap<Mesh, Uint8Array>()

/* "rgb(15, 107, 92)" or "#0f6b5c" as numbers; a colour that cannot be read is the page's accent. */
export function colourParts(colour: string): [number, number, number] {
  const hex = /^#([0-9a-f]{6})$/i.exec(colour.trim())
  if (hex) return [0, 2, 4].map(i => parseInt(hex[1].slice(i, i + 2), 16)) as [number, number, number]
  const parts = colour.match(/[\d.]+/g)
  if (parts && parts.length >= 3) return [Number(parts[0]), Number(parts[1]), Number(parts[2])]
  return [15, 107, 92]
}

const tone = (base: [number, number, number], towards: number, amount: number) =>
  'rgb(' + base.map(v => Math.round(v + (towards - v) * amount)).join(',') + ')'

/* One frame: the mesh turned about the vertical axis, seen a little from above, the far
   triangles first. Every triangle of a face gets the same solid shade, and only the outline of
   a face is drawn, so a face is one surface. */
export function drawShape(context: CanvasRenderingContext2D, width: number, height: number,
  shape: Mesh, size: number[], angle: number, color: string) {
  context.clearRect(0, 0, width, height)
  const { vertices, triangles } = shape
  const count = vertices.length / 3
  if (!count || !triangles.length) return
  let outline = outlines.get(shape)
  if (!outline) outlines.set(shape, outline = featureEdges(shape))
  const [cx, cy, cz] = size.map(v => v / 2)
  const radius = Math.hypot(cx, cy, cz) || 1
  const scale = Math.min(width, height) * 0.46 / radius
  const cos = Math.cos(angle), sin = Math.sin(angle), up = Math.cos(TILT), down = Math.sin(TILT)
  const px = new Float32Array(count), py = new Float32Array(count), depth = new Float32Array(count)
  for (let i = 0; i < count; i++) {
    const x = vertices[i * 3] - cx, y = vertices[i * 3 + 1] - cy, z = vertices[i * 3 + 2] - cz
    const rx = x * cos - y * sin, ry = x * sin + y * cos
    px[i] = width / 2 + rx * scale
    py[i] = height / 2 - (z * up - ry * down) * scale
    depth[i] = ry * up + z * down
  }
  const faces: { index: number; depth: number; light: number }[] = []
  for (let t = 0; t < triangles.length; t += 3) {
    const a = triangles[t], b = triangles[t + 1], c = triangles[t + 2]
    // The normal in view space; its sign is not trusted (meshes come with either winding).
    const ux = px[b] - px[a], uy = py[b] - py[a], uz = depth[b] - depth[a]
    const vx = px[c] - px[a], vy = py[c] - py[a], vz = depth[c] - depth[a]
    const nx = uy * vz - uz * vy, ny = uz * vx - ux * vz, nz = ux * vy - uy * vx
    const length = Math.hypot(nx, ny, nz)
    if (!length) continue
    const light = Math.abs(nx * 0.35 + ny * -0.5 + nz * 0.8) / length
    faces.push({ index: t, depth: depth[a] + depth[b] + depth[c], light })
  }
  faces.sort((first, second) => first.depth - second.depth)
  const base = colourParts(color)
  const edge = tone(base, 0, 0.35)
  context.globalAlpha = 1
  context.lineJoin = 'round'
  context.lineCap = 'round'
  for (const face of faces) {
    const a = triangles[face.index], b = triangles[face.index + 1], c = triangles[face.index + 2]
    // Two decimals: the triangles of one face get the very same colour, so no seam shows.
    const shade = tone(base, 255, Math.round(Math.min(1, face.light) * 60) / 100)
    context.fillStyle = shade
    context.strokeStyle = shade
    context.lineWidth = 0.75                   // covers the hairline between neighbours
    context.beginPath()
    context.moveTo(px[a], py[a])
    context.lineTo(px[b], py[b])
    context.lineTo(px[c], py[c])
    context.closePath()
    context.fill()
    context.stroke()
    const corners = [a, b, c]
    let drawing = false
    for (let s = 0; s < 3; s++) {
      if (!outline[face.index + s]) continue
      if (!drawing) {
        context.strokeStyle = edge
        context.lineWidth = 1
        context.beginPath()
        drawing = true
      }
      context.moveTo(px[corners[s]], py[corners[s]])
      context.lineTo(px[corners[(s + 1) % 3]], py[corners[(s + 1) % 3]])
    }
    if (drawing) context.stroke()
  }
}
