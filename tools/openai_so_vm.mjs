#!/usr/bin/env node
/**
 * Protocol-mode OpenAI Sentinel SO VM runner.
 *
 * This script does not open Chrome/CDP. It executes the Sentinel session
 * observer collector/snapshot bytecode returned by /backend-api/sentinel/req
 * inside a small browser-like JS environment and prints:
 *   {"ok":true,"so":"...","collector_result":"...","snapshot_result":"..."}
 *
 * Input is JSON on stdin:
 * {
 *   "p": "gAAAAAC...",
 *   "collector_dx": "...",
 *   "snapshot_dx": "...",
 *   "device_id": "...",
 *   "flow": "authorize_continue",
 *   "user_agent": "...",
 *   "href": "https://auth.openai.com/log-in-or-create-account"
 * }
 */

import { webcrypto, randomUUID } from "node:crypto";
import { performance } from "node:perf_hooks";
import { Buffer } from "node:buffer";

function readStdin() {
  return new Promise((resolve, reject) => {
    let body = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      body += chunk;
    });
    process.stdin.on("end", () => resolve(body));
    process.stdin.on("error", reject);
  });
}

function b64Encode(value) {
  return Buffer.from(String(value), "utf8").toString("base64");
}

function b64Decode(value) {
  return Buffer.from(String(value), "base64").toString("utf8");
}

function makeStorage() {
  const map = new Map();
  return {
    get length() {
      return map.size;
    },
    key(index) {
      return [...map.keys()][Number(index) || 0] ?? null;
    },
    getItem(key) {
      key = String(key);
      return map.has(key) ? map.get(key) : null;
    },
    setItem(key, value) {
      map.set(String(key), String(value));
    },
    removeItem(key) {
      map.delete(String(key));
    },
    clear() {
      map.clear();
    },
  };
}

function makeEventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(listener);
    },
    removeEventListener(type, listener) {
      listeners.get(type)?.delete(listener);
    },
    dispatchEvent(event) {
      const type = String(event?.type || "");
      for (const listener of listeners.get(type) || []) {
        try {
          listener.call(this, event);
        } catch {}
      }
      return true;
    },
    __listeners: listeners,
  };
}

function makeSyntheticEvent(type, payload = {}) {
  return {
    type,
    bubbles: true,
    cancelable: true,
    isTrusted: true,
    timeStamp: performance.now(),
    target: globalThis.document || null,
    currentTarget: null,
    defaultPrevented: false,
    preventDefault() {
      this.defaultPrevented = true;
    },
    stopPropagation() {},
    stopImmediatePropagation() {},
    ...payload,
  };
}

function dispatchSyntheticEvent(type, payload = {}) {
  const event = makeSyntheticEvent(type, payload);
  try {
    globalThis.dispatchEvent?.(event);
  } catch {}
  try {
    globalThis.document?.dispatchEvent?.(event);
  } catch {}
  try {
    globalThis.document?.body?.dispatchEvent?.(event);
  } catch {}
}

async function dispatchSyntheticEventAsync(type, payload = {}) {
  const event = makeSyntheticEvent(type, payload);
  const listeners = [...(globalThis.__listeners?.get(type) || [])];
  for (const listener of listeners) {
    try {
      event.currentTarget = globalThis;
      await Promise.resolve(listener.call(globalThis, event));
    } catch {}
  }
}

async function simulateSessionObserverActivity() {
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const moves = [
    [183, 214],
    [241, 238],
    [318, 266],
    [407, 303],
    [526, 347],
    [618, 382],
    [711, 421],
  ];
  for (const [clientX, clientY] of moves) {
    await dispatchSyntheticEventAsync("pointermove", {
      pointerType: "mouse",
      pointerId: 1,
      isPrimary: true,
      clientX,
      clientY,
      screenX: clientX + 120,
      screenY: clientY + 80,
      movementX: 3,
      movementY: 2,
      buttons: 0,
      altKey: false,
      ctrlKey: false,
      metaKey: false,
      shiftKey: false,
    });
    await sleep(8);
  }
  await dispatchSyntheticEventAsync("click", {
    pointerType: "mouse",
    clientX: 711,
    clientY: 421,
    screenX: 831,
    screenY: 501,
    button: 0,
    buttons: 0,
    detail: 1,
    altKey: false,
    ctrlKey: false,
    metaKey: false,
    shiftKey: false,
  });
  await sleep(12);
  for (const key of ["Shift", "A", "Backspace", "Tab"]) {
    await dispatchSyntheticEventAsync("keydown", {
      key,
      code: key === "Backspace" ? "Backspace" : key === "Tab" ? "Tab" : `Key${key}`,
      altKey: false,
      ctrlKey: false,
      metaKey: false,
      shiftKey: key === "Shift",
      repeat: false,
    });
    await sleep(6);
  }
  await dispatchSyntheticEventAsync("wheel", {
    deltaX: 0,
    deltaY: 146,
    deltaZ: 0,
    deltaMode: 0,
    clientX: 622,
    clientY: 446,
  });
  globalThis.scrollY = 146;
  globalThis.pageYOffset = 146;
  await dispatchSyntheticEventAsync("scroll", {
    target: globalThis.document,
  });
  await sleep(8);
  await dispatchSyntheticEventAsync("paste", {
    clipboardData: {
      getData(type) {
        return String(type || "").toLowerCase() === "text" ? "x" : "";
      },
    },
  });
  await sleep(8);
}

function hydrateSessionObserverState() {
  const now = performance.now();
  const setIfEmpty = (key, value) => {
    if (globalThis[key] === undefined || globalThis[key] === null) globalThis[key] = value;
  };
  const pointerPath = [
    [183, 214, 0],
    [241, 238, 18],
    [318, 266, 42],
    [407, 303, 69],
    [526, 347, 104],
    [618, 382, 139],
    [711, 421, 181],
  ];
  setIfEmpty("__oai_so_t0", Math.max(1, Math.round(now - 850)));
  setIfEmpty("__oai_so_p", pointerPath);
  setIfEmpty("__oai_so_pc", pointerPath.length);
  setIfEmpty("__oai_so_m", pointerPath.slice(-5));
  setIfEmpty("__oai_so_lx", 711);
  setIfEmpty("__oai_so_ly", 421);
  setIfEmpty("__oai_so_sx0", 183);
  setIfEmpty("__oai_so_sy0", 214);
  setIfEmpty("__oai_so_c", 1);
  setIfEmpty("__oai_so_cn", 1);
  setIfEmpty("__oai_so_bc", 1);
  setIfEmpty("__oai_so_bm", 0);
  setIfEmpty("__oai_so_k", [
    ["Shift", 0, 0, 0, 1, 220],
    ["A", 0, 0, 0, 0, 246],
    ["Backspace", 0, 0, 0, 0, 293],
    ["Tab", 0, 0, 0, 0, 338],
  ]);
  setIfEmpty("__oai_so_kp", 4);
  setIfEmpty("__oai_so_s", [[0, 146, 382]]);
  setIfEmpty("__oai_so_sp", 146);
  setIfEmpty("__oai_so_spt", 382);
  setIfEmpty("__oai_so_ss", 1);
  setIfEmpty("__oai_so_ss2", 1);
  setIfEmpty("__oai_so_sw", [[0, 146, 0, 382]]);
  setIfEmpty("__oai_so_wb", 1);
  setIfEmpty("__oai_so_we", 146);
  setIfEmpty("__oai_so_wl", [[0, 146, 622, 446, 382]]);
  setIfEmpty("__oai_so_h", 1);
  setIfEmpty("__oai_so_hi", 1);
  setIfEmpty("__oai_so_hp", 0);
  setIfEmpty("__oai_so_hw", 0);
  setIfEmpty("__oai_so_ht", 0);
  setIfEmpty("__oai_so_hc", 0);
  setIfEmpty("__oai_so_i", 1);
  setIfEmpty("__oai_so_fn", 1);
  setIfEmpty("__oai_so_fs", 0);
  setIfEmpty("__oai_so_fs2", 0);
  setIfEmpty("__oai_so_sn", 0);
  setIfEmpty("__oai_so_st", 0);
  setIfEmpty("__oai_so_cs", 0);
  setIfEmpty("__oai_so_cs2", 0);
}

function makeCanvas() {
  const ctx2d = {
    fillStyle: "#000000",
    strokeStyle: "#000000",
    font: "10px sans-serif",
    globalCompositeOperation: "source-over",
    fillRect() {},
    clearRect() {},
    beginPath() {},
    closePath() {},
    moveTo() {},
    lineTo() {},
    arc() {},
    stroke() {},
    fill() {},
    fillText() {},
    strokeText() {},
    measureText(text) {
      return { width: String(text || "").length * 7.1 };
    },
    getImageData(width = 0, height = 0) {
      const size = Math.max(0, Number(width) * Number(height) * 4) || 4;
      return { data: new Uint8ClampedArray(size) };
    },
    putImageData() {},
    createLinearGradient() {
      return { addColorStop() {} };
    },
  };
  const gl = {
    VERSION: 0x1f02,
    VENDOR: 0x1f00,
    RENDERER: 0x1f01,
    UNMASKED_VENDOR_WEBGL: 0x9245,
    UNMASKED_RENDERER_WEBGL: 0x9246,
    getExtension(name) {
      if (String(name || "").toLowerCase() === "webgl_debug_renderer_info") {
        return {
          UNMASKED_VENDOR_WEBGL: 0x9245,
          UNMASKED_RENDERER_WEBGL: 0x9246,
        };
      }
      return null;
    },
    getParameter(param) {
      if (param === 0x1f00 || param === 0x9245) return "Google Inc. (Intel)";
      if (param === 0x1f01 || param === 0x9246) return "ANGLE (Intel, Intel(R) UHD Graphics Direct3D11 vs_5_0 ps_5_0, D3D11)";
      if (param === 0x1f02) return "WebGL 1.0 (OpenGL ES 2.0 Chromium)";
      return 0;
    },
    getSupportedExtensions() {
      return ["WEBGL_debug_renderer_info", "EXT_texture_filter_anisotropic"];
    },
  };
  return {
    tagName: "CANVAS",
    width: 300,
    height: 150,
    style: {},
    getContext(type) {
      const value = String(type || "").toLowerCase();
      if (value === "2d") return ctx2d;
      if (value.includes("webgl")) return gl;
      return null;
    },
    toDataURL() {
      return "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAFgwJ/lmM7WQAAAABJRU5ErkJggg==";
    },
    addEventListener() {},
    removeEventListener() {},
    getBoundingClientRect() {
      return { x: 0, y: 0, top: 0, left: 0, right: 300, bottom: 150, width: 300, height: 150 };
    },
  };
}

function installBrowserLikeGlobals(input) {
  const userAgent =
    String(input.user_agent || "").trim() ||
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36";
  const href = String(input.href || "").trim() || "https://auth.openai.com/log-in-or-create-account";
  const url = new URL(href);
  const eventTarget = makeEventTarget();
  const cookieJar = new Map();
  if (input.device_id) cookieJar.set("oai-did", String(input.device_id));

  const documentElement = {
    tagName: "HTML",
    style: {},
    dataset: {},
    getAttribute(name) {
      if (String(name) === "data-build") return "";
      return null;
    },
    setAttribute() {},
    removeAttribute() {},
    appendChild() {},
    getBoundingClientRect() {
      return { x: 0, y: 0, top: 0, left: 0, right: 1920, bottom: 1080, width: 1920, height: 1080 };
    },
  };
  const body = {
    tagName: "BODY",
    style: {},
    appendChild(node) {
      return node;
    },
    removeChild(node) {
      return node;
    },
    getBoundingClientRect() {
      return { x: 0, y: 0, top: 0, left: 0, right: 1920, bottom: 1080, width: 1920, height: 1080 };
    },
  };
  const sentinelScript = {
    src: "https://sentinel.openai.com/sentinel/20260219f9f6/sdk.js",
    type: "text/javascript",
    async: true,
    defer: true,
    getAttribute(name) {
      if (String(name) === "src") return this.src;
      return null;
    },
  };
  const document = {
    URL: href,
    documentURI: href,
    referrer: "https://platform.openai.com/",
    title: "OpenAI",
    compatMode: "CSS1Compat",
    readyState: "complete",
    visibilityState: "visible",
    hidden: false,
    currentScript: sentinelScript,
    scripts: [sentinelScript],
    documentElement,
    body,
    head: {
      tagName: "HEAD",
      appendChild(node) {
        return node;
      },
      removeChild(node) {
        return node;
      },
    },
    get cookie() {
      return [...cookieJar.entries()].map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("; ");
    },
    set cookie(value) {
      const first = String(value || "").split(";")[0] || "";
      const idx = first.indexOf("=");
      if (idx > 0) cookieJar.set(first.slice(0, idx).trim(), decodeURIComponent(first.slice(idx + 1)));
    },
    createElement(tag) {
      tag = String(tag || "").toLowerCase();
      if (tag === "canvas") return makeCanvas();
      const nodeEvents = makeEventTarget();
      return {
        tagName: tag.toUpperCase(),
        style: {},
        children: [],
        src: "",
        href: "",
        async: false,
        defer: false,
        contentWindow: null,
        appendChild(child) {
          this.children.push(child);
          return child;
        },
        removeChild(child) {
          this.children = this.children.filter((item) => item !== child);
          return child;
        },
        setAttribute(name, value) {
          this[String(name)] = String(value);
        },
        getAttribute(name) {
          return this[String(name)] ?? null;
        },
        addEventListener: nodeEvents.addEventListener,
        removeEventListener: nodeEvents.removeEventListener,
        dispatchEvent: nodeEvents.dispatchEvent,
        getBoundingClientRect() {
          return { x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0 };
        },
      };
    },
    querySelector() {
      return null;
    },
    querySelectorAll() {
      return [];
    },
    getElementsByTagName(tag) {
      if (String(tag || "").toLowerCase() === "script") return this.scripts;
      return [];
    },
    addEventListener: eventTarget.addEventListener,
    removeEventListener: eventTarget.removeEventListener,
    dispatchEvent: eventTarget.dispatchEvent,
    hasFocus() {
      return true;
    },
  };

  const navigatorProto = {
    javaEnabled() {
      return false;
    },
    vibrate() {
      return false;
    },
  };
  const navigator = Object.create(navigatorProto);
  Object.assign(navigator, {
    userAgent,
    appVersion: userAgent,
    language: "en-US",
    languages: ["en-US", "en"],
    platform: "Win32",
    vendor: "Google Inc.",
    vendorSub: "",
    product: "Gecko",
    productSub: "20030107",
    hardwareConcurrency: 8,
    deviceMemory: 8,
    maxTouchPoints: 0,
    cookieEnabled: true,
    onLine: true,
    webdriver: false,
    doNotTrack: "1",
    plugins: [],
    mimeTypes: [],
    permissions: {
      query() {
        return Promise.resolve({ state: "prompt", onchange: null });
      },
    },
    connection: {
      effectiveType: "4g",
      downlink: 10,
      rtt: 50,
      saveData: false,
    },
    userAgentData: {
      brands: [
        { brand: "Google Chrome", version: "145" },
        { brand: "Chromium", version: "145" },
        { brand: "Not/A)Brand", version: "99" },
      ],
      mobile: false,
      platform: "Windows",
      getHighEntropyValues(keys) {
        const values = {
          architecture: "x86",
          bitness: "64",
          model: "",
          platformVersion: "10.0.0",
          uaFullVersion: "145.0.0.0",
          fullVersionList: [
            { brand: "Google Chrome", version: "145.0.0.0" },
            { brand: "Chromium", version: "145.0.0.0" },
            { brand: "Not/A)Brand", version: "99.0.0.0" },
          ],
        };
        return Promise.resolve(Object.fromEntries((keys || []).map((key) => [key, values[key] ?? ""])));
      },
    },
  });

  const location = {
    href,
    origin: url.origin,
    protocol: url.protocol,
    host: url.host,
    hostname: url.hostname,
    port: url.port,
    pathname: url.pathname,
    search: url.search,
    hash: url.hash,
    assign(next) {
      this.href = String(next);
    },
    replace(next) {
      this.href = String(next);
    },
    reload() {},
    toString() {
      return this.href;
    },
  };

  const window = globalThis;
  Object.assign(window, eventTarget);
  const setGlobal = (name, value) => {
    Object.defineProperty(window, name, {
      value,
      writable: true,
      configurable: true,
      enumerable: true,
    });
  };
  window.window = window;
  window.self = window;
  window.globalThis = window;
  window.top = window;
  window.parent = window;
  window.frames = window;
  setGlobal("document", document);
  setGlobal("navigator", navigator);
  setGlobal("location", location);
  setGlobal("screen", {
    width: 1920,
    height: 1080,
    availWidth: 1920,
    availHeight: 1040,
    colorDepth: 24,
    pixelDepth: 24,
    orientation: { type: "landscape-primary", angle: 0 },
  });
  window.innerWidth = 1280;
  window.innerHeight = 900;
  window.outerWidth = 1280;
  window.outerHeight = 900;
  window.scrollX = 0;
  window.scrollY = 0;
  window.pageXOffset = 0;
  window.pageYOffset = 0;
  window.devicePixelRatio = 1;
  window.localStorage = makeStorage();
  window.sessionStorage = makeStorage();
  setGlobal("performance", performance);
  if (!window.performance.memory) {
    Object.defineProperty(window.performance, "memory", {
      value: {
      jsHeapSizeLimit: 4294705152,
      totalJSHeapSize: 10000000,
      usedJSHeapSize: 5000000,
      },
      configurable: true,
    });
  }
  setGlobal("crypto", webcrypto);
  window.crypto.randomUUID = window.crypto.randomUUID || randomUUID;
  window.atob = window.atob || b64Decode;
  window.btoa = window.btoa || b64Encode;
  window.TextEncoder = TextEncoder;
  window.TextDecoder = TextDecoder;
  window.URL = URL;
  window.URLSearchParams = URLSearchParams;
  window.requestAnimationFrame = (cb) => setTimeout(() => cb(performance.now()), 16);
  window.cancelAnimationFrame = (id) => clearTimeout(id);
  window.requestIdleCallback = (cb, options = {}) =>
    setTimeout(() => cb({ timeRemaining: () => 50, didTimeout: false }), Math.min(10, Number(options.timeout) || 1));
  window.cancelIdleCallback = (id) => clearTimeout(id);
  window.chrome = {
    runtime: {},
    loadTimes() {
      return {};
    },
    csi() {
      return {};
    },
    app: {},
  };
  window.indexedDB = {};
  window.caches = {};
  window.Notification = { permission: "default" };
  window.open = () => null;
  window.close = () => {};
  window.focus = () => {};
  window.blur = () => {};
  window.getComputedStyle = () => ({ getPropertyValue: () => "", display: "block", visibility: "visible" });
  window.matchMedia = (query) => ({
    matches: false,
    media: String(query || ""),
    onchange: null,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {
      return true;
    },
  });
  window.fetch = async () => {
    throw new Error("fetch_disabled_in_so_vm");
  };
}

function Tt() {
  const t = [
    "abs",
    "snapshot_dx",
    "toString",
    "src",
    "splice",
    "isArray",
    "fromCharCode",
    "length",
    "clear",
    "map",
    "set",
    "session_observer_vm_timeout",
    "collector_dx",
    "then",
    "parse",
    "match",
    "filter",
    "function",
    "finally",
    "apply",
    "resolve",
    "indexOf",
    "charCodeAt",
    "bind",
    "catch",
    "get",
    "(((.+)+)+)+$",
  ];
  return (Tt = function () {
    return t;
  })();
}

function xt(t, n) {
  const e = Tt();
  return (xt = function (t, n) {
    return e[(t -= 0)];
  })(t, n);
}

const J = 0,
  G = 1,
  W = 2,
  z = 3,
  H = 4,
  V = 5,
  B = 6,
  Z = 24,
  K = 7,
  Q = 8,
  Y = 9,
  X = 10,
  tt = 11,
  nt = 12,
  et = 13,
  rt = 14,
  ot = 15,
  it = 16,
  ct = 17,
  st = 18,
  ut = 19,
  at = 23,
  ft = 20,
  lt = 21,
  dt = 22,
  ht = 25,
  pt = 26,
  mt = 27,
  gt = 28,
  wt = 29,
  yt = 30,
  vt = 33,
  bt = 34,
  kt = 35,
  St = new Map();
let Ct = 0,
  At = Promise.resolve();

function Ot(t) {
  const n = xt,
    e = At[n(13)](t, t);
  return (At = e[n(13)](
    () => {},
    () => {},
  )), e;
}

async function _t() {
  const t = xt;
  for (; St[t(25)](Y).length > 0; ) {
    const [n, ...e] = St[t(25)](Y).shift(),
      r = St.get(n)(...e);
    r && typeof r[t(13)] === t(17) && (await r), Ct++;
  }
}

function Rt(t, n) {
  const e = xt;
  let r = "";
  for (let o = 0; o < t[e(7)]; o++) r += String[e(6)](t[e(22)](o) ^ n[e(22)](o % n[e(7)]));
  return r;
}

function Nt(t) {
  return Ot(() => jt(t));
}

function jt(t, n) {
  return new Promise((e, r) => {
    const o = xt;
    void 0 !== n &&
      ((function () {
        const t = xt;
        St[t(8)](),
          St.set(J, Nt),
          St[t(10)](G, (n, e) => St[t(10)](n, Rt("" + St[t(25)](n), "" + St.get(e)))),
          St[t(10)](W, (n, e) => St[t(10)](n, e)),
          St[t(10)](V, (n, e) => {
            const r = t,
              o = St[r(25)](n);
            Array[r(5)](o) ? o.push(St[r(25)](e)) : St.set(n, o + St.get(e));
          }),
          St.set(mt, (n, e) => {
            const r = t,
              o = St.get(n);
            Array.isArray(o) ? o[r(4)](o[r(21)](St.get(e)), 1) : St[r(10)](n, o - St[r(25)](e));
          }),
          St.set(wt, (n, e, r) => St.set(n, St[t(25)](e) < St[t(25)](r))),
          St.set(vt, (n, e, r) => {
            const o = t,
              i = Number(St[o(25)](e)),
              c = Number(St[o(25)](r));
            St[o(10)](n, i * c);
          }),
          St.set(kt, (n, e, r) => {
            const o = t,
              i = Number(St.get(e)),
              c = Number(St[o(25)](r));
            St[o(10)](n, 0 === c ? 0 : i / c);
          }),
          St[t(10)](B, (n, e, r) => {
            const prop = St.get(r);
            const source = St[t(25)](e);
            const value = source?.[prop];
            if (globalThis.__SO_TRACE && typeof prop === "string" && prop.startsWith("__oai_so_")) {
              globalThis.__SO_TRACE.push({
                op: "getprop",
                prop,
                value: Array.isArray(value) ? { type: "array", length: value.length, sample: value.slice(0, 3) } : value,
              });
            }
            St.set(n, value);
          }),
          St.set(K, (n, ...e) => {
            const fn = St[t(25)](n);
            const args = e[t(9)]((n) => St[t(25)](n));
            if (globalThis.__SO_TRACE && (fn === Reflect.set || fn === Reflect.get)) {
              const prop = args[1];
              if (typeof prop === "string" && prop.startsWith("__oai_so_")) {
                globalThis.__SO_TRACE.push({
                  op: fn === Reflect.set ? "reflect_set_direct" : "reflect_get_direct",
                  prop,
                  value: Array.isArray(args[2]) ? { type: "array", length: args[2].length, sample: args[2].slice(0, 3) } : args[2],
                });
              }
            }
            return fn(...args);
          }),
          St[t(10)](ct, (n, e, ...r) => {
            const o = t;
            try {
              const fn = St[o(25)](e);
              const args = r[o(9)]((t) => St[o(25)](t));
              if (globalThis.__SO_TRACE && (fn === Reflect.set || fn === Reflect.get)) {
                const prop = args[1];
                if (typeof prop === "string" && prop.startsWith("__oai_so_")) {
                  globalThis.__SO_TRACE.push({
                    op: fn === Reflect.set ? "reflect_set" : "reflect_get",
                    prop,
                    value: Array.isArray(args[2]) ? { type: "array", length: args[2].length, sample: args[2].slice(0, 3) } : args[2],
                  });
                }
              }
              const t = fn(...args);
              if (t && typeof t.then === o(17))
                return t[o(13)]((t) => {
                  St.set(n, t);
                }).catch((t) => {
                  St.set(n, "" + t);
                });
              St[o(10)](n, t);
            } catch (t) {
              St[o(10)](n, "" + t);
            }
          }),
          St[t(10)](et, (n, e, ...r) => {
            const o = t;
            try {
              St[o(25)](e)(...r[o(9)]((t) => St[o(25)](t)));
            } catch (t) {
              St[o(10)](n, "" + t);
            }
          }),
          St.set(Q, (n, e) => St[t(10)](n, St[t(25)](e))),
          St[t(10)](X, window),
          St[t(10)](tt, (n, e) =>
            St[t(10)](
              n,
              (Array.from(document.scripts || [])
                [t(9)]((n) => n?.[t(3)]?.[t(15)](St[t(25)](e)))
                [t(16)]((n) => n?.[t(7)])[0] ?? [])[0] ?? null,
            ),
          ),
          St[t(10)](nt, (n) => St[t(10)](n, St)),
          St[t(10)](rt, (n, e) => St[t(10)](n, JSON[t(14)]("" + St[t(25)](e)))),
          St.set(ot, (n, e) => St[t(10)](n, JSON.stringify(St.get(e)))),
          St[t(10)](st, (n) => St[t(10)](n, atob("" + St[t(25)](n)))),
          St[t(10)](ut, (n) => St[t(10)](n, btoa("" + St[t(25)](n)))),
          St[t(10)](ft, (n, e, r, ...o) => (St[t(25)](n) === St[t(25)](e) ? St[t(25)](r)(...o) : null)),
          St[t(10)](lt, (n, e, r, o, ...i) =>
            Math[t(0)](St[t(25)](n) - St.get(e)) > St[t(25)](r) ? St[t(25)](o)(...i) : null,
          ),
          St[t(10)](at, (n, e, ...r) => (void 0 !== St[t(25)](n) ? St[t(25)](e)(...r) : null)),
          St[t(10)](Z, (n, e, r) => St[t(10)](n, St.get(e)[St.get(r)][t(23)](St.get(e)))),
          St[t(10)](bt, (n, e) => {
            const r = t;
            try {
              const t = St.get(e);
              return Promise[r(20)](t)[r(13)]((t) => {
                St.set(n, t);
              });
            } catch {
              return;
            }
          }),
          St[t(10)](dt, (n, e) => {
            const r = t,
              o = [...St[r(25)](Y)];
            return St[r(10)](Y, [...e]), _t()[r(24)]((t) => {
              St[r(10)](n, "" + t);
            })[r(18)](() => {
              St[r(10)](Y, o);
            });
          }),
          St[t(10)](gt, () => {}),
          St.set(pt, () => {}),
          St[t(10)](ht, () => {});
      })(),
      (Ct = 0),
      St.set(it, n));
    let i = false;
    const c = setTimeout(() => {
        !i && ((i = true), r(new Error(xt(11))));
      }, 6e4),
      s = (t) => {
        i || ((i = true), clearTimeout(c), e(t));
      };
    St[o(10)](z, (t) => {
      s(btoa("" + t));
    }),
      St.set(H, (t) => {
        ((t) => {
          i || ((i = true), clearTimeout(c), r(t));
        })(btoa("" + t));
      }),
      St[o(10)](yt, (t, n, e, r) => {
        const i = o,
          c = Array[i(5)](r),
          s = c ? e : [],
          u = (c ? r : e) || [];
        St[i(10)](t, (...t) => {
          const e = i,
            r = [...St[e(25)](Y)];
          if (c)
            for (let n = 0; n < s[e(7)]; n++) {
              const r = s[n],
                o = t[n];
              St[e(10)](r, o);
            }
          return St[e(10)](Y, [...u]), _t()[e(13)](() => St.get(n))[e(24)]((t) => "" + t)[e(18)](() => {
            St.set(Y, r);
          });
        });
      });
    try {
      St[o(10)](Y, JSON[o(14)](Rt(atob(t), "" + St[o(25)](it)))),
        _t()[o(24)]((t) => {
          s(btoa(Ct + ": " + t));
        });
    } catch (t) {
      s(btoa(Ct + ": " + t));
    }
  });
}

function looksLikeErrorResult(value) {
  try {
    const decoded = b64Decode(value);
    return /^\d+:\s/.test(decoded);
  } catch {
    return false;
  }
}

async function main() {
  const inputText = await readStdin();
  const input = JSON.parse(inputText || "{}");
  if (input.debug) globalThis.__SO_TRACE = [];
  const p = String(input.p || "");
  const collectorDx = String(input.collector_dx || "");
  const snapshotDx = String(input.snapshot_dx || "");
  const turnstileDx = String(input.turnstile_dx || "");
  if (!p || ((!collectorDx || !snapshotDx) && !turnstileDx)) {
    throw new Error("missing p and collector_dx/snapshot_dx or turnstile_dx");
  }

  installBrowserLikeGlobals(input);
  const turnstileResult = turnstileDx ? await jt(turnstileDx, p) : "";
  let collectorResult = "";
  let snapshotResult = "";
  let stateBeforeSnapshot = null;
  if (collectorDx && snapshotDx) {
    collectorResult = await jt(collectorDx, p);
    await simulateSessionObserverActivity();
    hydrateSessionObserverState();
    stateBeforeSnapshot = input.debug
    ? Object.fromEntries(
        Object.keys(globalThis)
          .filter((key) => key.startsWith("__oai_so_"))
          .sort()
          .map((key) => {
            const value = globalThis[key];
            return [key, Array.isArray(value) ? { type: "array", length: value.length, value: value.slice(0, 5) } : value];
          }),
      )
    : null;
    snapshotResult = await jt(snapshotDx);
  }
  const output = {
    ok: (!!snapshotResult && !looksLikeErrorResult(snapshotResult)) || (!!turnstileResult && !looksLikeErrorResult(turnstileResult)),
    so: snapshotResult || "",
    turnstile: turnstileResult || "",
    collector_result: collectorResult || "",
    snapshot_result: snapshotResult || "",
    turnstile_result_decoded_if_error: looksLikeErrorResult(turnstileResult) ? b64Decode(turnstileResult) : "",
    collector_result_decoded_if_error: looksLikeErrorResult(collectorResult) ? b64Decode(collectorResult) : "",
    snapshot_result_decoded_if_error: looksLikeErrorResult(snapshotResult) ? b64Decode(snapshotResult) : "",
    length: snapshotResult ? snapshotResult.length : 0,
  };
  if (input.debug) {
    output.state = Object.fromEntries(
      Object.keys(globalThis)
        .filter((key) => key.startsWith("__oai_so_"))
        .sort()
        .map((key) => {
          const value = globalThis[key];
          return [key, Array.isArray(value) ? { type: "array", length: value.length, value: value.slice(0, 5) } : value];
        }),
    );
    output.listener_counts = Object.fromEntries(
      [...(globalThis.__listeners || new Map()).entries()].map(([key, value]) => [key, value?.size || 0]),
    );
    output.state_before_snapshot = stateBeforeSnapshot;
    output.trace = globalThis.__SO_TRACE?.slice(-200);
  }
  process.stdout.write(`${JSON.stringify(output)}\n`);
}

main().catch((error) => {
  process.stdout.write(JSON.stringify({ ok: false, error: String(error?.stack || error) }) + "\n");
  process.exit(1);
});
