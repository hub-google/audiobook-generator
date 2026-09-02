import { BotGuardClient, getChallenge } from "bgutils-js/botguard";
import { buildURL, getHeaders, USER_AGENT } from "bgutils-js/utils";
import { WebPoMinter } from "bgutils-js/webpo";
import { JSDOM } from "jsdom";

const contentBinding = process.argv[2];
if (!contentBinding) throw new Error("Missing content binding");

const requestKey = "O43z0dpjhgX20SCx4KAo";
const dom = new JSDOM("<!doctype html><html><head></head><body></body></html>", {
  url: "https://www.youtube.com/",
  referrer: "https://www.youtube.com/",
  userAgent: USER_AGENT,
});
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  location: dom.window.location,
  origin: dom.window.origin,
});
if (!("navigator" in globalThis)) {
  Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator });
}

const challenge = await getChallenge({ fetchFunction: fetch, requestKey });
const interpreter = challenge.interpreterJavascript
  ?.privateDoNotAccessOrElseSafeScriptWrappedValue;
if (!interpreter) throw new Error("BotGuard interpreter unavailable");
new Function(interpreter)();

const client = await BotGuardClient.create({
  program: challenge.program,
  globalName: challenge.globalName,
  globalObject: globalThis,
});
const webPoSignalOutput = [];
const botguardResponse = await client.snapshot({ webPoSignalOutput });
const response = await fetch(buildURL("GenerateIT", true), {
  method: "POST",
  headers: getHeaders(),
  body: JSON.stringify([requestKey, botguardResponse]),
});
if (!response.ok) throw new Error(`GenerateIT HTTP ${response.status}`);
const [integrityToken, estimatedTtlSecs, mintRefreshThreshold, websafeFallbackToken]
  = await response.json();
const minter = await WebPoMinter.create(
  { integrityToken, estimatedTtlSecs, mintRefreshThreshold, websafeFallbackToken },
  webPoSignalOutput,
);
process.stdout.write(await minter.mintAsWebsafeString(contentBinding));
