// Saathi WhatsApp bridge — multi-user version.
// Forwards every incoming 1:1 message to Python with { chat_id, text }.
// /send and /send-file accept { chat_id, text/path/caption } in the body.
require("dotenv").config();
const express = require("express");
const qrcode = require("qrcode-terminal");
const { Client, LocalAuth, MessageMedia } = require("whatsapp-web.js");

const PORT = parseInt(process.env.BRIDGE_PORT || "3001", 10);
const PY_URL = process.env.PY_WEBHOOK || "http://127.0.0.1:8000/incoming";
const TOKEN = process.env.BRIDGE_TOKEN || "";
const CHROME_PATH = process.env.CHROME_PATH || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
// Optional: owner's own number for self-chat testing
const ADMIN_NUMBER = (process.env.ADMIN_NUMBER || "").replace(/\D/g, "");
const DEBUG_SELF = process.env.DEBUG_SELF === "1";

let client = null;
let ready = false;

async function isSelfChat(msg, c = client) {
  if (!msg || !msg.fromMe) return false;
  if (msg.to && msg.from && msg.to === msg.from) return true;

  const wid = c?.info?.wid?._serialized || (typeof c?.info?.wid === "string" ? c.info.wid : null);
  if (wid && msg.to === wid) return true;

  const lid = c?.info?.lid?._serialized || (typeof c?.info?.lid === "string" ? c.info.lid : null);
  if (lid && msg.to === lid) return true;

  if (ADMIN_NUMBER && msg.to) {
    const toNum = msg.to.split("@")[0].replace(/\D/g, "");
    if (toNum === ADMIN_NUMBER) return true;
  }

  try {
    if (typeof msg.getChat === "function") {
      const chat = await msg.getChat();
      if (chat && chat.isSelf) return true;
    }
  } catch (e) {
    // guard with try/catch since not every version has it
  }

  return false;
}

// Media sends to @lid chats can fail with
// "Data passed to getter must include an id property". Try three ways, in order.
async function sendMediaRobust(chat_id, media, caption) {
  const base = { sendSeen: false, sendMediaAsDocument: true };
  const attempts = [
    // 1. normal send, without marking the chat as seen
    () => client.sendMessage(chat_id, media, { ...base, caption }),
    // 2. look the chat up first and send through it
    async () => {
      const chat = await client.getChatById(chat_id);
      return chat.sendMessage(media, { ...base, caption });
    },
    // 3. last resort: file first, caption as a separate text message
    async () => {
      await client.sendMessage(chat_id, media, base);
      if (caption) await client.sendMessage(chat_id, caption);
    },
  ];
  let last;
  for (let i = 0; i < attempts.length; i++) {
    try {
      await attempts[i]();
      if (i > 0) console.log(`[SEND-FILE] worked with attempt ${i + 1}`);
      return;
    } catch (e) {
      last = e;
      console.error(`[SEND-FILE] attempt ${i + 1} failed: ${e.message}`);
    }
  }
  throw last;
}

// Windows: a hard-stopped bridge often leaves its hidden Chrome running, which locks the saved
// WhatsApp session and makes the next start hang. Kill only Chrome processes that use our session folder.
function killStaleChrome() {
  if (process.platform !== "win32") return;
  try {
    require("child_process").execFileSync("powershell", [
      "-NoProfile", "-Command",
      "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | " +
      "Where-Object { $_.CommandLine -like '*wwebjs_auth*' } | " +
      "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }",
    ], { stdio: "ignore", timeout: 20000 });
    console.log("[START] checked for leftover bridge Chrome processes");
  } catch (e) {
    console.log("[START] leftover-Chrome check skipped:", e.message);
  }
}

if (require.main === module) {
  killStaleChrome();
  client = new Client({
    authStrategy: new LocalAuth({ dataPath: "./.wwebjs_auth" }),
    puppeteer: {
      headless: true,
      executablePath: CHROME_PATH,
      args: ["--no-sandbox", "--disable-setuid-sandbox"],
    },
  });

  client.on("qr", (qr) => {
    console.log("Scan QR with WhatsApp > Linked devices:");
    qrcode.generate(qr, { small: true });
  });

  client.on("ready", () => {
    ready = true;
    const wid = client.info?.wid?._serialized || null;
    const lid = client.info?.lid?._serialized || (typeof client.info?.lid === "string" ? client.info.lid : null);
    console.log(`WhatsApp ready. Own WID: ${wid}, LID: ${lid}`);
  });

  client.on("disconnected", (r) => {
    ready = false;
    console.error("Disconnected:", r);
  });

  client.on("message_create", async (msg) => {
    try {
      // Skip groups and status broadcasts
      if (!msg.from || msg.from.endsWith("@g.us") || msg.from === "status@broadcast") return;

      let chat_id = null;
      let is_self = false;

      if (!msg.fromMe) {
        // Normal incoming message from another person
        chat_id = msg.from;
      } else {
        is_self = await isSelfChat(msg, client);
        if (is_self) {
          // Ignore own bot replies in self-chat
          if (msg.body && msg.body.startsWith("🤖")) return;
          chat_id = msg.to;
        } else {
          // Outgoing message to someone else — ignore
          if (DEBUG_SELF) {
            const toSuffix = (msg.to || "").slice(-6);
            const fromSuffix = (msg.from || "").slice(-6);
            const ownSuffix = (client?.info?.wid?._serialized || "").slice(-6);
            console.log(`[SELF-DEBUG] fromMe to-suffix=${toSuffix} from-suffix=${fromSuffix} own=${ownSuffix} matched=false`);
          }
          return;
        }
      }

      if (!msg.body) return;

      // Log only suffix and length — never the content
      const suffix = chat_id.slice(-6);
      console.log(`[MSG] ...${suffix} len=${msg.body.length}`);

      const payload = { chat_id, text: msg.body };
      if (is_self) {
        payload.is_self = true;
      }

      const resp = await fetch(PY_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", "x-token": TOKEN },
        body: JSON.stringify(payload),
      });
      if (!resp.ok) {
        console.error(`[FWD] Python error ${resp.status} for ...${suffix}`);
      }
    } catch (e) {
      console.error("forward failed:", e.message);
    }
  });

  const app = express();
  app.use(express.json({ limit: "1mb" }));
  app.use((req, res, next) =>
    req.get("x-token") === TOKEN ? next() : res.status(401).json({ error: "bad token" })
  );

  app.post("/send", async (req, res) => {
    if (!ready) return res.status(503).json({ error: "not ready" });
    const { chat_id, text } = req.body;
    if (!chat_id) return res.status(400).json({ error: "chat_id required" });
    try {
      await client.sendMessage(chat_id, "🤖 " + text);
      res.json({ ok: true });
    } catch (e) {
      console.error("[SEND ERROR]", e.message);
      res.status(500).json({ error: e.message });
    }
  });

  app.post("/send-file", async (req, res) => {
    if (!ready) return res.status(503).json({ error: "not ready" });
    const { chat_id, path: filePath, caption } = req.body;
    if (!chat_id) return res.status(400).json({ error: "chat_id required" });
    try {
      const media = MessageMedia.fromFilePath(filePath);
      await sendMediaRobust(chat_id, media, "🤖 " + (caption || ""));
      res.json({ ok: true });
    } catch (e) {
      console.error("[SEND-FILE ERROR]", e.message);
      res.status(500).json({ error: e.message });
    }
  });

  app.get("/health", (_req, res) => {
    const wid = client?.info?.wid?._serialized || null;
    const lid = client?.info?.lid?._serialized || (typeof client?.info?.lid === "string" ? client.info.lid : null);
    res.json({ ready, wid, lid });
  });

  // Progress logging, so a hang shows where it stops
  client.on("loading_screen", (p, m) => console.log(`[LOADING] ${p}% ${m}`));
  client.on("authenticated", () => console.log("[AUTH] authenticated"));
  client.on("auth_failure", (m) => console.error("[AUTH FAILURE]", m));
  client.on("change_state", (s) => console.log("[STATE]", s));

  // Close Chrome properly on Ctrl+C so it is not left running
  let shuttingDown = false;
  async function shutdown(sig) {
    if (shuttingDown) return;
    shuttingDown = true;
    console.log(`[STOP] ${sig}: closing WhatsApp...`);
    try {
      await Promise.race([client.destroy(), new Promise((r) => setTimeout(r, 8000))]);
    } catch (e) { /* ignore */ }
    process.exit(0);
  }
  process.on("SIGINT", () => shutdown("SIGINT"));
  process.on("SIGTERM", () => shutdown("SIGTERM"));

  // If WhatsApp is not ready in 2 minutes, say so and exit instead of hanging silently
  const watchdog = setTimeout(() => {
    if (!ready) {
      console.error("[TIMEOUT] WhatsApp not ready after 120s. Close this window, then run npm start again. " +
        "If it repeats, paste the [LOADING]/[AUTH]/[STATE] lines above.");
      process.exit(1);
    }
  }, 120000);
  client.on("ready", () => clearTimeout(watchdog));

  client.initialize().catch((e) => {
    console.error("[INIT ERROR]", e.message);
    process.exit(1);
  });
  app.listen(PORT, "127.0.0.1", () => console.log(`Bridge on 127.0.0.1:${PORT}`));
}

module.exports = { isSelfChat, ADMIN_NUMBER };