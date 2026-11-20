#!/usr/bin/env node
// Flicker check for 3D play-checks (z-fighting, depth shimmer, shadow acne).
//
// The check loads a page in headless Chromium, and a driver module moves the
// camera or steps an animation by a small amount for each frame. The check
// then finds "oscillating" pixels: a pixel that changes A -> B -> A across 3
// frames in sequence. Monotonic motion almost never makes this pattern.
// Z-fighting makes it all the time.
//
// Usage:
//   node flicker-check.mjs --url URL --driver driver.mjs [--out DIR]
//        [--frames 30] [--threshold 0.05] [--size 1280x720] [--diff 48]
//
// The driver is an ES module:
//   export async function setup(page) {}    // optional: wait for load, freeze
//                                           // the game clock and randomness
//   export const scenarios = [              // one or more spots to check
//     { name: "tram-doors",
//       async frame(page, i, n) {}          // set the state for frame i of n
//     },
//   ];
// The check waits 2 animation frames after each frame() call. If the game
// only renders from its own loop, frame() must call the render function or
// step the loop.
//
// Output, for each scenario: the peak and mean percentage of oscillating
// pixels, a heat map PNG (red = oscillation, on a dark copy of the first
// frame) and the first frame as a PNG. A "frozen" check also captures frame 0
// two times. If the 2 captures are different, the scene is not frozen and the
// result is not reliable.
// Exit code: 0 = all scenarios under the threshold, 1 = flicker found,
// 2 = error or scene not frozen.

import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";
import zlib from "node:zlib";
import { pathToFileURL } from "node:url";

const require = createRequire(import.meta.url);

function parseArgs(argv) {
  const a = { frames: 30, threshold: 0.05, size: "1280x720", diff: 48, out: "flicker-out" };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    if (!k.startsWith("--")) throw new Error(`unexpected argument: ${k}`);
    a[k.slice(2)] = argv[++i];
  }
  if (!a.url || !a.driver) throw new Error("--url and --driver are required");
  a.frames = Math.max(3, parseInt(a.frames, 10));
  a.threshold = parseFloat(a.threshold);
  a.diff = parseInt(a.diff, 10);
  const [w, h] = a.size.split("x").map(Number);
  a.width = w;
  a.height = h;
  return a;
}

// --- PNG decode / encode (8-bit RGB or RGBA, no interlace: Chromium output) ---

function decodePng(buf) {
  let p = 8, width, height, colorType;
  const idat = [];
  while (p < buf.length) {
    const len = buf.readUInt32BE(p);
    const type = buf.toString("latin1", p + 4, p + 8);
    const data = buf.subarray(p + 8, p + 8 + len);
    if (type === "IHDR") {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      const depth = data[8];
      colorType = data[9];
      if (depth !== 8 || (colorType !== 2 && colorType !== 6) || data[12] !== 0)
        throw new Error(`unsupported PNG (depth ${depth}, color type ${colorType})`);
    } else if (type === "IDAT") idat.push(data);
    else if (type === "IEND") break;
    p += 12 + len;
  }
  const bpp = colorType === 6 ? 4 : 3;
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const stride = width * bpp;
  const px = Buffer.alloc(height * stride);
  for (let y = 0; y < height; y++) {
    const f = raw[y * (stride + 1)];
    const src = y * (stride + 1) + 1;
    const out = y * stride;
    for (let x = 0; x < stride; x++) {
      const r = raw[src + x];
      const left = x >= bpp ? px[out + x - bpp] : 0;
      const up = y > 0 ? px[out - stride + x] : 0;
      const ul = y > 0 && x >= bpp ? px[out - stride + x - bpp] : 0;
      let v;
      if (f === 0) v = r;
      else if (f === 1) v = r + left;
      else if (f === 2) v = r + up;
      else if (f === 3) v = r + ((left + up) >> 1);
      else if (f === 4) {
        const pa = Math.abs(up - ul), pb = Math.abs(left - ul), pc = Math.abs(left + up - 2 * ul);
        v = r + (pa <= pb && pa <= pc ? left : pb <= pc ? up : ul);
      } else throw new Error(`bad PNG filter ${f}`);
      px[out + x] = v & 255;
    }
  }
  // Normalize to RGB.
  if (bpp === 3) return { width, height, rgb: px };
  const rgb = Buffer.alloc(width * height * 3);
  for (let i = 0, j = 0; i < px.length; i += 4, j += 3) {
    rgb[j] = px[i]; rgb[j + 1] = px[i + 1]; rgb[j + 2] = px[i + 2];
  }
  return { width, height, rgb };
}

const CRC_TABLE = (() => {
  const t = new Int32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c;
  }
  return t;
})();

function crc32(buf) {
  let c = -1;
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 255] ^ (c >>> 8);
  return (c ^ -1) >>> 0;
}

function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const td = Buffer.concat([Buffer.from(type, "latin1"), data]);
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(td));
  return Buffer.concat([len, td, crc]);
}

function encodePng(width, height, rgb) {
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; ihdr[9] = 2; // 8-bit RGB
  const raw = Buffer.alloc(height * (width * 3 + 1));
  for (let y = 0; y < height; y++) rgb.copy(raw, y * (width * 3 + 1) + 1, y * width * 3, (y + 1) * width * 3);
  return Buffer.concat([
    Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]),
    chunk("IHDR", ihdr),
    chunk("IDAT", zlib.deflateSync(raw)),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

// --- analysis ---

function maxDiff(a, b, i) {
  return Math.max(Math.abs(a[i] - b[i]), Math.abs(a[i + 1] - b[i + 1]), Math.abs(a[i + 2] - b[i + 2]));
}

function changedPct(a, b, diff) {
  let n = 0;
  for (let i = 0; i < a.length; i += 3) if (maxDiff(a, b, i) >= diff) n++;
  return (100 * n) / (a.length / 3);
}

// A pixel oscillates at frame k if it changes a lot from k-1 to k and from k
// to k+1, but frame k+1 is close to frame k-1 again.
function oscillation(frames, diff) {
  const pixels = frames[0].length / 3;
  const heat = new Uint16Array(pixels);
  const perTriple = [];
  for (let k = 1; k < frames.length - 1; k++) {
    const [a, b, c] = [frames[k - 1], frames[k], frames[k + 1]];
    let n = 0;
    for (let p = 0, i = 0; p < pixels; p++, i += 3) {
      if (maxDiff(a, b, i) >= diff && maxDiff(b, c, i) >= diff && maxDiff(a, c, i) < diff / 2) {
        n++;
        heat[p]++;
      }
    }
    perTriple.push((100 * n) / pixels);
  }
  const peak = Math.max(...perTriple);
  const mean = perTriple.reduce((s, v) => s + v, 0) / perTriple.length;
  return { peak, mean, heat };
}

function heatMap(base, heat) {
  const out = Buffer.alloc(base.length);
  let max = 1;
  for (const h of heat) if (h > max) max = h;
  for (let p = 0, i = 0; p < heat.length; p++, i += 3) {
    if (heat[p]) {
      const t = 0.5 + (0.5 * heat[p]) / max;
      out[i] = Math.round(255 * t); out[i + 1] = 0; out[i + 2] = 0;
    } else {
      out[i] = base[i] >> 2; out[i + 1] = base[i + 1] >> 2; out[i + 2] = base[i + 2] >> 2;
    }
  }
  return out;
}

// --- main ---

async function settle(page) {
  await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))));
}

async function capture(page) {
  return decodePng(await page.screenshot({ type: "png" }));
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const driver = await import(pathToFileURL(path.resolve(args.driver)).href);
  if (!Array.isArray(driver.scenarios) || !driver.scenarios.length)
    throw new Error("the driver must export a non-empty `scenarios` array");
  fs.mkdirSync(args.out, { recursive: true });

  const { chromium } = require("playwright");
  const browser = await chromium.launch({ args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] });
  const page = await browser.newPage({ viewport: { width: args.width, height: args.height }, deviceScaleFactor: 1 });
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => m.type() === "error" && errors.push(m.text()));

  let exit = 0;
  try {
    await page.goto(args.url, { waitUntil: "load" });
    if (driver.setup) await driver.setup(page);
    for (const sc of driver.scenarios) {
      const slug = sc.name.replace(/[^a-z0-9_-]+/gi, "-");
      const frames = [];
      let width, height;
      for (let i = 0; i < args.frames; i++) {
        await sc.frame(page, i, args.frames);
        await settle(page);
        const img = await capture(page);
        ({ width, height } = img);
        frames.push(img.rgb);
        if (i === 0) {
          // Frozen check: the same state must give the same pixels.
          await sc.frame(page, 0, args.frames);
          await settle(page);
          const again = await capture(page);
          const drift = changedPct(img.rgb, again.rgb, args.diff);
          if (drift > 0.01) {
            console.log(`${sc.name}: NOT FROZEN (${drift.toFixed(3)}% of pixels change with no motion). Freeze the clock and randomness in setup().`);
            exit = 2;
          }
        }
      }
      const { peak, mean, heat } = oscillation(frames, args.diff);
      fs.writeFileSync(path.join(args.out, `${slug}-frame0.png`), encodePng(width, height, frames[0]));
      fs.writeFileSync(path.join(args.out, `${slug}-heat.png`), encodePng(width, height, heatMap(frames[0], heat)));
      const moved = changedPct(frames[0], frames[frames.length - 1], args.diff);
      const verdict = peak > args.threshold ? "FLICKER" : "ok";
      if (verdict === "FLICKER" && exit === 0) exit = 1;
      console.log(
        `${sc.name}: ${verdict} peak ${peak.toFixed(3)}% mean ${mean.toFixed(3)}% oscillating ` +
          `(threshold ${args.threshold}%, ${args.frames} frames, ${moved.toFixed(1)}% of pixels moved first->last) ` +
          `heat map: ${path.join(args.out, `${slug}-heat.png`)}`,
      );
      if (moved < 0.05) console.log(`${sc.name}: warning: almost nothing moved; check that frame() moves the camera or animation.`);
    }
  } finally {
    await browser.close();
  }
  if (errors.length) console.log(`console errors:\n  ${errors.join("\n  ")}`);
  process.exit(exit);
}

main().catch((e) => {
  console.error(`flicker-check: ${e.stack || e}`);
  process.exit(2);
});
