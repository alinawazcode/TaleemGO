// Sathi WhatsApp bridge (unofficial, whatsapp-web.js).
// Python does all the thinking; this file only moves messages in and out.
require("dotenv").config();
const express = require("express");
const qrcode = require("qrcode-terminal");
const { Client, LocalAuth, MessageMedia } = require("whatsapp-web.js");

const PORT = parseInt(process.env.BRIDGE_PORT || "3001", 10);
const PY_URL = process.env.PY_WEBHOOK || "http://127.0.0.1:8000/incoming";
const TOKEN = process.env.BRIDGE_TOKEN || "";
const ALLOWED = (process.env.WA_ALLOWED_NUMBER || "").replace(/\D/g, ""); // e.g. 923001234567
const PREFIX = "🤖 "; // every bot message starts with this, so we never answer ourselves

if (!ALLOWED) { console.error("Set WA_ALLOWED_NUMBER in .env"); process.exit(1); }

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: "./.wwebjs_auth" }),
  puppeteer: { headless: true, args: ["--no-sandbox", "--disable-setuid-sandbox"] },
});

let ready = false;
client.on("qr", (qr) => { console.log("Scan this QR with WhatsApp > Linked devices:"); qrcode.generate(qr, { small: true }); });
client.on("ready", () => { ready = true; console.log("WhatsApp ready"); });
client.on("disconnected", (r) => { ready = false; console.error("Disconnected:", r); });

client.on("message_create", async (msg) => {
  try {
    const chatId = msg.fromMe ? msg.to : msg.from;
    if (!chatId || chatId.endsWith("@g.us")) return;          // ignore groups
    if (chatId.split("@")[0] !== ALLOWED) return;              // only the authorised chat
    if (msg.fromMe && (msg.body || "").startsWith(PREFIX.trim())) return; // our own replies
    if (!msg.body) return;
    await fetch(PY_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-token": TOKEN },
      body: JSON.stringify({ text: msg.body }),
    });
  } catch (e) { console.error("forward failed:", e.message); }
});

const target = `${ALLOWED}@c.us`;
const app = express();
app.use(express.json({ limit: "1mb" }));
app.use((req, res, next) => (req.get("x-token") === TOKEN ? next() : res.status(401).json({ error: "bad token" })));

app.post("/send", async (req, res) => {
  if (!ready) return res.status(503).json({ error: "not ready" });
  try { await client.sendMessage(target, PREFIX + req.body.text); res.json({ ok: true }); }
  catch (e) { res.status(500).json({ error: e.message }); }
});

app.post("/send-file", async (req, res) => {
  if (!ready) return res.status(503).json({ error: "not ready" });
  try {
    const media = MessageMedia.fromFilePath(req.body.path);
    await client.sendMessage(target, media, { caption: PREFIX + (req.body.caption || ""), sendMediaAsDocument: true });
    res.json({ ok: true });
  } catch (e) { res.status(500).json({ error: e.message }); }
});

app.get("/health", (_req, res) => res.json({ ready }));

client.initialize();
app.listen(PORT, "127.0.0.1", () => console.log(`Bridge on 127.0.0.1:${PORT}`));