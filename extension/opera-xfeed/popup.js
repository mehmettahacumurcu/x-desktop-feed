const code = document.getElementById("pairing-code");
const status = document.getElementById("status");
const lang = document.getElementById("lang");

const STRINGS = {
  en: {
    appName: "X Desktop Feed",
    codeLabel: "Pairing code",
    pair: "Pair",
    check: "Check connection",
    openX: "Open X",
    language: "Language",
    checking: "Checking connection…",
    notPaired: "Not paired",
    connectedX: "Connected — X: {x}",
    pairedDisconnected: "Paired, disconnected — X: {x}",
    pairing: "Pairing…",
    paired: "Paired",
    pairingFailed: "Pairing failed",
    disconnected: "Disconnected",
    couldNotOpenX: "Could not open X",
  },
  tr: {
    appName: "X Desktop Feed",
    codeLabel: "Eşleştirme kodu",
    pair: "Eşleştir",
    check: "Bağlantıyı kontrol et",
    openX: "X'i Aç",
    language: "Dil",
    checking: "Bağlantı kontrol ediliyor…",
    notPaired: "Eşleştirilmedi",
    connectedX: "Bağlı — X: {x}",
    pairedDisconnected: "Eşleştirildi, bağlantı yok — X: {x}",
    pairing: "Eşleştiriliyor…",
    paired: "Eşleştirildi",
    pairingFailed: "Eşleştirme başarısız",
    disconnected: "Bağlantı kesik",
    couldNotOpenX: "X açılamadı",
  },
};

const LANG_KEY = "lang";
let current = "en";

function t(key, values = {}) {
  const table = STRINGS[current] || STRINGS.en;
  let template = table[key] || STRINGS.en[key] || key;
  for (const [name, value] of Object.entries(values)) {
    template = template.replaceAll(`{${name}}`, value);
  }
  return template;
}

function translatePage() {
  for (const element of document.querySelectorAll("[data-i18n]")) {
    if (element.dataset.i18n === "status") continue;
    element.textContent = t(element.dataset.i18n);
  }
  document.documentElement.lang = current;
}

async function loadLanguage() {
  try {
    const stored = await chrome.storage.local.get(LANG_KEY);
    if (stored[LANG_KEY] === "tr" || stored[LANG_KEY] === "en") current = stored[LANG_KEY];
  } catch {}
  lang.value = current;
  translatePage();
}

function show(value) {
  status.textContent = value;
}

async function message(payload) {
  try { return await chrome.runtime.sendMessage(payload); }
  catch (error) { return { ok: false, error: String(error?.message || error) }; }
}

async function refresh() {
  show(t("checking"));
  const result = await message({ type: "status" });
  if (result.paired) {
    show(result.connected
      ? t("connectedX", { x: result.x_state })
      : t("pairedDisconnected", { x: result.x_state }));
  } else {
    show(t("notPaired"));
  }
}

document.getElementById("pair").addEventListener("click", async () => {
  show(t("pairing"));
  const result = await message({ type: "pair", code: code.value });
  code.value = "";
  show(result.ok ? t("paired") : result.error || t("pairingFailed"));
});

document.getElementById("check").addEventListener("click", async () => {
  show(t("checking"));
  const result = await message({ type: "refresh" });
  show(result.connected
    ? t("connectedX", { x: result.x_state })
    : result.diagnostic || t("disconnected"));
});

document.getElementById("open-x").addEventListener("click", async () => {
  const result = await message({ type: "open-x" });
  if (!result.ok) show(result.error || t("couldNotOpenX"));
});

lang.addEventListener("change", async () => {
  current = lang.value;
  translatePage();
  try { await chrome.storage.local.set({ [LANG_KEY]: current }); } catch {}
});

loadLanguage().then(refresh);