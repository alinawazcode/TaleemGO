// Sathi WhatsApp bridge (unofficial, whatsapp-web.js).
// Python does all the thinking; this file only moves messages in and out.
require("dotenv").config();
const express = require("express");
const qrcode = require("qrcode-terminal");
const { Client, LocalAuth, MessageMedia } = require("whatsapp-web.js");

const PORT = parseInt(process.env.BRIDGE_PORT || "3001", 10);
const PY_URL = process.env.PY_WEBHOOK || "http://127.0.0.1:8000/incoming";
const TOKEN = process.env.BRIDGE_TOKEN || "";
const ALLOWED = (process.env.WA_ALLOWED_NUMBER || "").replace(/\D/g, "");
const PREFIX = "🤖 ";

if (!ALLOWED) { console.error("Set WA_ALLOWED_NUMBER in .env"); process.exit(1); }

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: "./.wwebjs_auth" }),
  puppeteer: {
    headless: true,
    executablePath: process.env.CHROME_PATH || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
    args: ["--no-sandbox", "--disable-setuid-sandbox"],
  },
});

let ready = false;
let targetChatId = null; // full serialized ID e.g. 263939246014664@lid or @c.us

client.on("qr", (qr) => { console.log("Scan QR with WhatsApp > Linked devices:"); qrcode.generate(qr, { small: true }); });
client.on("ready", () => { ready = true; console.log("WhatsApp ready"); });
client.on("disconnected", (r) => { ready = false; console.error("Disconnected:", r); });

client.on("message_create", async (msg) => {
  try {
    // Full serialized chat ID — may be @c.us or @lid for self-chat
    const chatId = msg.fromMe ? msg.to : msg.from;
    const num = chatId ? chatId.split("@")[0] : "";

    console.log(`[MSG] fromMe=${msg.fromMe} num=${num} chatId=${chatId} body="${(msg.body || "").slice(0, 40)}"`);

    if (!chatId || chatId.endsWith("@g.us")) return;           // ignore groups
    if (num !== ALLOWED) { console.log(`[SKIP] num mismatch`); return; }
    if (msg.fromMe && (msg.body || "").startsWith(PREFIX.trim())) return; // own bot replies
    if (!msg.body) return;

    // Capture the exact chat ID (preserving @lid / @c.us) from first real message
    if (!targetChatId) {
      targetChatId = chatId;
      console.log(`[INIT] targetChatId locked to: ${targetChatId}`);
    }

    console.log(`[FWD] -> Python: "${msg.body.slice(0, 40)}"`);
    const resp = await fetch(PY_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-token": TOKEN },
      body: JSON.stringify({ text: msg.body }),
    });
    console.log(`[FWD] Python responded: ${resp.status}`);
  } catch (e) { console.error("forward failed:", e.message); }
});

const app = express();
app.use(express.json({ limit: "1mb" }));
app.use((req, res, next) => (req.get("x-token") === TOKEN ? next() : res.status(401).json({ error: "bad token" })));

app.post("/send", async (req, res) => {
  if (!ready) return res.status(503).json({ error: "not ready" });
  if (!targetChatId) return res.status(503).json({ error: "no message received yet — send a message first to initialise the chat" });
  try {
    console.log(`[SEND] -> ${targetChatId}: "${(req.body.text || "").slice(0, 40)}"`);
    await client.sendMessage(targetChatId, PREFIX + req.body.text);
    res.json({ ok: true });
  } catch (e) {
    console.error("[SEND ERROR]", e.message);
    res.status(500).json({ error: e.message });
  }
});

app.post("/send-file", async (req, res) => {
  if (!ready) return res.status(503).json({ error: "not ready" });
  if (!targetChatId) return res.status(503).json({ error: "no message received yet" });
  try {
    const media = MessageMedia.fromFilePath(req.body.path);
    await client.sendMessage(targetChatId, media, { caption: PREFIX + (req.body.caption || ""), sendMediaAsDocument: true });
    res.json({ ok: true });
  } catch (e) {
    console.error("[SEND-FILE ERROR]", e.message);
    res.status(500).json({ error: e.message });
  }
});

app.get("/health", (_req, res) => res.json({ ready, targetChatId }));

client.initialize();
app.listen(PORT, "127.0.0.1", () => console.log(`Bridge on 127.0.0.1:${PORT}`));
