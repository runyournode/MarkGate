// Minimal tar reader (ustar + PAX + GNU long names), regular files only.
// Exposes window.MarkgateTar.parse(Uint8Array) -> [{ name, data: Uint8Array }]
(function () {
  "use strict";

  const BLOCK = 512;
  const utf8 = new TextDecoder("utf-8");

  function readString(buf, offset, length) {
    const slice = buf.subarray(offset, offset + length);
    const end = slice.indexOf(0);
    return utf8.decode(end === -1 ? slice : slice.subarray(0, end));
  }

  function readOctal(buf, offset, length) {
    const str = readString(buf, offset, length).trim();
    return str ? parseInt(str, 8) : 0;
  }

  // PAX records: "<len> <key>=<value>\n", <len> counted in bytes (values are UTF-8)
  function parsePax(data) {
    const out = {};
    let pos = 0;
    while (pos < data.length) {
      const space = data.indexOf(0x20, pos);
      if (space === -1) break;
      const len = parseInt(utf8.decode(data.subarray(pos, space)), 10);
      if (!len) break;
      const record = utf8.decode(data.subarray(space + 1, pos + len - 1));
      const eq = record.indexOf("=");
      if (eq !== -1) out[record.slice(0, eq)] = record.slice(eq + 1);
      pos += len;
    }
    return out;
  }

  function parse(buf) {
    const files = [];
    let offset = 0;
    let paxNext = {};
    let paxGlobal = {};
    let longName = null;

    while (offset + BLOCK <= buf.length) {
      const header = buf.subarray(offset, offset + BLOCK);
      if (header.every((b) => b === 0)) break; // end-of-archive marker

      let name = readString(header, 0, 100);
      const size = readOctal(header, 124, 12);
      const type = String.fromCharCode(header[156] || 48); // NUL -> "0"
      if (readString(header, 257, 5) === "ustar") {
        const prefix = readString(header, 345, 155);
        if (prefix) name = prefix + "/" + name;
      }

      const dataStart = offset + BLOCK;
      const data = buf.subarray(dataStart, dataStart + size);
      offset = dataStart + Math.ceil(size / BLOCK) * BLOCK;

      if (type === "x") { paxNext = parsePax(data); continue; }
      if (type === "g") { paxGlobal = { ...paxGlobal, ...parsePax(data) }; continue; }
      if (type === "L") { longName = readString(data, 0, data.length); continue; }

      const pax = { ...paxGlobal, ...paxNext };
      if (pax.path) name = pax.path;
      if (longName) name = longName;
      paxNext = {};
      longName = null;

      if (type === "0" || type === "7") files.push({ name, data });
    }
    return files;
  }

  window.MarkgateTar = { parse };
})();
