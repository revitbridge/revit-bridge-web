import { describe, expect, it } from 'vitest'
import { boxMesh, colourParts, drawShape, featureEdges, sizeText, type Mesh } from './shape'

/* A canvas that records what was drawn: every fill with its colour, every stroked path with
   its colour and its points. */
function fakeContext() {
  const calls: string[] = []
  const fills: string[] = []
  const fillByPath = new Map<string, string>()
  const strokes: { colour: string; points: number[][] }[] = []
  let path: number[][] = []
  const context = {
    globalAlpha: 0.5, fillStyle: '', strokeStyle: '', lineJoin: '', lineCap: '', lineWidth: 1,
    clearRect: (...args: number[]) => { calls.push('clear(' + args.join(',') + ')') },
    beginPath: () => { path = [] },
    moveTo: (x: number, y: number) => { path.push([x, y]) },
    lineTo: (x: number, y: number) => { path.push([x, y]) },
    closePath: () => {},
    fill: () => {
      fills.push(context.fillStyle)
      fillByPath.set(JSON.stringify(path), context.fillStyle)
    },
    stroke: () => { strokes.push({ colour: context.strokeStyle, points: path.slice() }) },
  }
  return { context: context as unknown as CanvasRenderingContext2D, calls, fills, fillByPath, strokes, raw: context }
}

function prismMesh(sides: number): Mesh {
  const bottom = Array.from({ length: sides }, (_, i) => {
    const angle = 2 * Math.PI * i / sides
    return [500 + 500 * Math.cos(angle), 500 + 500 * Math.sin(angle), 0]
  })
  const top = bottom.map(([x, y]) => [x, y, 1000])
  const bottomCentre = 2 * sides, topCentre = bottomCentre + 1
  const vertices = [...bottom.flat(), ...top.flat(), 500, 500, 0, 500, 500, 1000]
  const triangles: number[] = []
  for (let i = 0; i < sides; i++) {
    const j = (i + 1) % sides
    const a = vertices.length / 3
    vertices.push(...bottom[i], ...bottom[j], ...top[j], ...top[i])
    // Two side triangles of opposite winding, then one triangle from each cap.
    triangles.push(a, a + 1, a + 2, a, a + 3, a + 2,
      bottomCentre, j, i, topCentre, sides + i, sides + j)
  }
  return { vertices, triangles }
}

/* Identify each triangle's fill by its projected path, independent of painter order. */
function triangleColours(mesh: Mesh, size: number[]) {
  function draw(shape: Mesh) {
    const { context, fillByPath } = fakeContext()
    drawShape(context, 160, 120, shape, size, 0.6, '#000000')
    return fillByPath
  }
  const fills = draw(mesh)
  const colours: string[] = []
  for (let t = 0; t < mesh.triangles.length; t += 3) {
    const [path] = draw({ vertices: mesh.vertices, triangles: mesh.triangles.slice(t, t + 3) }).keys()
    const colour = fills.get(path)
    if (!colour) throw new Error('Missing fill for triangle ' + t / 3)
    colours.push(colour)
  }
  return colours
}

describe('the rough element shape', () => {
  it('turns a size into a closed box of twelve triangles', () => {
    const box = boxMesh([4000, 200, 3000])
    expect(box.vertices).toHaveLength(24)
    expect(box.triangles).toHaveLength(36)
    expect(Math.max(...box.triangles)).toBe(7)
    expect(Math.max(...box.vertices)).toBe(4000)
    // A size of zero still gives something to draw.
    expect(Math.min(...boxMesh([0, 0, 0]).vertices.filter(v => v > 0))).toBe(1)
  })

  it('writes the size in whole millimetres', () => {
    expect(sizeText([4000.4, 200, 2999.6])).toBe('4000 × 200 × 3000 mm')
  })

  it('reads a colour as numbers', () => {
    expect(colourParts('rgb(15, 107, 92)')).toEqual([15, 107, 92])
    expect(colourParts('#0F6B5C')).toEqual([15, 107, 92])
    expect(colourParts('canvastext')).toEqual([15, 107, 92])
  })

  it('takes the diagonal of a flat face for the inside of the face', () => {
    // Each side of a box is two triangles: their two outer sides are outline, the diagonal is not.
    const flags = featureEdges(boxMesh([4000, 200, 3000]))
    expect(flags).toHaveLength(36)
    expect([...flags].filter(Boolean)).toHaveLength(24)
    for (let t = 0; t < 12; t++) expect(flags[t * 3] + flags[t * 3 + 1] + flags[t * 3 + 2]).toBe(2)
  })

  it('matches corners by position, as the triangles of different faces come with their own numbers', () => {
    // One flat square as two triangles that share no corner number: still one face.
    const square = { vertices: [0, 0, 0, 10, 0, 0, 10, 10, 0, 0, 0, 0, 10, 10, 0, 0, 10, 0], triangles: [0, 1, 2, 3, 4, 5] }
    expect([...featureEdges(square)]).toEqual([1, 1, 0, 0, 1, 1])
    // Folded along the shared side, it is two faces and the fold is drawn.
    const folded = { ...square, vertices: [0, 0, 0, 10, 0, 0, 10, 10, 0, 0, 0, 0, 10, 10, 0, 0, 10, 10] }
    expect([...featureEdges(folded)]).toEqual([1, 1, 1, 1, 1, 1])
    // A side of an open sheet belongs to one triangle only: outline.
    expect([...featureEdges({ vertices: [0, 0, 0, 10, 0, 0, 0, 10, 0], triangles: [0, 1, 2] })]).toEqual([1, 1, 1])
  })

  it('fills the two triangles of a face in one solid colour and draws only the outline', () => {
    const { context, calls, fills, strokes, raw } = fakeContext()
    drawShape(context, 160, 120, boxMesh([4000, 200, 3000]), [4000, 200, 3000], 0.6, 'rgb(15, 107, 92)')
    expect(calls).toEqual(['clear(0,0,160,120)'])
    expect(fills).toHaveLength(12)
    expect(new Set(fills).size).toBeLessThanOrEqual(3)            // opposite sides share a shade
    for (const colour of fills) expect(colour).toMatch(/^rgb\(\d+,\d+,\d+\)$/)
    expect(raw.globalAlpha).toBe(1)                               // solid, nothing shows through
    const edge = 'rgb(10,70,60)'
    const outline = strokes.filter(stroke => stroke.colour === edge)
    const seams = strokes.filter(stroke => stroke.colour !== edge)
    expect(outline).toHaveLength(12)                              // one path per triangle,
    for (const stroke of outline) expect(stroke.points).toHaveLength(4)   // two sides each: never the diagonal
    expect(seams).toHaveLength(12)
    for (const stroke of seams) expect(fills).toContain(stroke.colour)    // seams are covered in the face's own colour
    for (const stroke of strokes) for (const [x, y] of stroke.points) {
      expect(x).toBeGreaterThanOrEqual(0); expect(x).toBeLessThanOrEqual(160)
      expect(y).toBeGreaterThanOrEqual(0); expect(y).toBeLessThanOrEqual(120)
    }
  })

  it.each([[32, 0], [10, 1]])('marks the rims and classifies every strip boundary of a %i-sided prism', (sides, vertical) => {
    const flags = featureEdges(prismMesh(sides))
    for (let i = 0; i < sides; i++) {
      expect([...flags.slice(i * 12, (i + 1) * 12)]).toEqual([
        1, vertical, 0, vertical, 1, 0, // both side rims, both verticals, neither diagonal
        0, 1, 0, 0, 1, 0,              // only the rim of each cap triangle
      ])
    }
  })

  it('changes shade gradually across adjacent prism strips with mixed winding', () => {
    const sides = 32
    const colours = triangleColours(prismMesh(sides), [1000, 1000, 1000])
    const sideColours = colours.filter((_, triangle) => triangle % 4 < 2)
    expect(new Set(sideColours).size).toBeGreaterThan(2)
    const steps = Array.from({ length: sides }, (_, i) => {
      // The first triangle touches the second triangle of the next strip along a vertical.
      const right = colourParts(sideColours[i * 2])[0]
      const left = colourParts(sideColours[((i + 1) % sides) * 2 + 1])[0]
      return Math.abs(right - left)
    })
    expect(Math.max(...steps)).toBeGreaterThan(0)
    expect(Math.max(...steps)).toBeLessThanOrEqual(12) // less than 5% of the grayscale range
  })

  it('gives both triangles of each box face exactly the same fill', () => {
    const size = [4000, 200, 3000]
    const colours = triangleColours(boxMesh(size), size)
    for (let i = 0; i < colours.length; i += 2) expect(colours[i]).toBe(colours[i + 1])
  })

  it('draws nothing for an empty mesh, and skips a triangle without area', () => {
    const empty = fakeContext()
    drawShape(empty.context, 160, 120, { vertices: [], triangles: [] }, [1, 1, 1], 0, '#000000')
    expect(empty.calls).toEqual(['clear(0,0,160,120)'])
    expect(empty.fills).toHaveLength(0)
    const flat = fakeContext()
    drawShape(flat.context, 160, 120, { vertices: [0, 0, 0, 0, 0, 0, 0, 0, 0], triangles: [0, 1, 2] }, [1, 1, 1], 0, '#000000')
    expect(flat.fills).toHaveLength(0)
  })
})
