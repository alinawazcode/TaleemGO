// Sathi WhatsApp bridge — multi-user version.
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
// Optional: owner's own number for self-chat testing (fromMe messages to self)
const ADMIN_NUMBER = (process.env.ADMIN_NUMBER || "").replace(/\D/g, "");

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: "./.wwebjs_auth" }),
  puppeteer: {
    headless: true,
    executablePath: CHROME_PATH,
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  },
});

let ready = false;

client.on("qr", (qr) => {
  console.log("Scan QR with WhatsApp > Linked devices:");
  qrcode.generate(qr, { small: true });
});
client.on("ready", () => { ready = true; console.log("WhatsApp ready"); });
client.on("disconnected", (r) => { ready = false; console.error("Disconnected:", r); });

client.on("message_create", async (msg) => {
  try {
    // Skip groups and status broadcasts
    if (!msg.from || msg.from.endsWith("@g.us") || msg.from === "status@broadcast") return;

    let chat_id = null;

    if (!msg.fromMe) {
      // Normal incoming message from another person
      chat_id = msg.from;
    } else if (ADMIN_NUMBER && msg.to && msg.to.split("@")[0] === ADMIN_NUMBER) {
      // Owner messaging themselves (self-chat) for testing
      chat_id = msg.to;
    } else {
      // Outgoing message to someone else — ignore
      return;
    }

    if (!msg.body) return;

    // Log only suffix and length — never the content
    const suffix = chat_id.slice(-6);
    console.log(`[MSG] ...${suffix} len=${msg.body.length}`);

    const resp = await fetch(PY_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-token": TOKEN },
      body: JSON.stringify({ chat_id, text: msg.body }),
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
    await client.sendMessage(chat_id, media, {
      caption: "🤖 " + (caption || ""),
      sendMediaAsDocument: true,
    });
    res.json({ ok: true });
  } catch (e) {
    console.error("[SEND-FILE ERROR]", e.message);
    res.status(500).json({ error: e.message });
  }
});

app.get("/health", (_req, res) => res.json({ ready }));

client.initialize();
app.listen(PORT, "127.0.0.1", () => console.log(`Bridge on 127.0.0.1:${PORT}`));
