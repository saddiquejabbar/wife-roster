import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";

import { createInboundController } from "./controller.js";
import { createTranscriptionExtractor } from "./extraction.js";
import { registerWifeRosterInteractiveHandler } from "./interactive.js";
import { createWorkflowRunner } from "./workflow.js";

const DEFAULT_EXTRACTION_MODEL = "openai/gpt-5.6-sol";

export default definePluginEntry({
  id: "wife-roster-inbound",
  name: "wife-roster inbound",
  description: "Thin, allowlisted Telegram review and approval hook for wife-roster.",
  register(api) {
    const config = resolveConfig(api.pluginConfig);
    if (config === null) {
      api.logger.info?.("wife-roster inbound is installed but not configured");
      return;
    }
    const controller = createInboundController({
      config,
      extractTranscription: createTranscriptionExtractor(api, config),
      runWorkflow: createWorkflowRunner(config),
    });

    registerWifeRosterInteractiveHandler(api, controller);

    api.on("reply_dispatch", async (event, hookContext) => {
      let reply;
      try {
        reply = await controller.handle(event.ctx);
      } catch {
        api.logger.warn?.("wife-roster inbound request failed safely");
        reply = "wife-roster could not complete this request safely.";
      }
      if (reply === null) {
        return;
      }
      const payload = typeof reply === "string" ? { text: reply } : reply;
      const queuedFinal = hookContext.dispatcher.sendFinalReply(payload);
      hookContext.recordProcessed("completed", { reason: "wife_roster_inbound" });
      hookContext.markIdle("message_completed");
      return {
        handled: true,
        queuedFinal: Boolean(queuedFinal),
        counts: hookContext.dispatcher.getQueuedCounts(),
      };
    }, { timeoutMs: 600000 });
  },
});


function resolveConfig(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const requiredStrings = [
    "groupId",
    "botUsername",
    "pythonBin",
    "appSrc",
    "vendorPath",
    "promptPath",
    "workingDirectory",
  ];
  for (const name of requiredStrings) {
    if (typeof value[name] !== "string" || !value[name].trim()) {
      return null;
    }
  }
  const allowedSenderIds = normalizeSenderIds(value.allowedSenderIds);
  if (allowedSenderIds === null) {
    return null;
  }
  const stateChangeSenderIds = value.stateChangeSenderIds === undefined
    ? allowedSenderIds
    : normalizeSenderIds(value.stateChangeSenderIds);
  if (stateChangeSenderIds === null) {
    return null;
  }
  const extractionModel = typeof value.extractionModel === "string" && value.extractionModel.trim()
    ? value.extractionModel.trim()
    : DEFAULT_EXTRACTION_MODEL;
  return {
    ...value,
    groupId: value.groupId.trim(),
    botUsername: value.botUsername.replace(/^@/, "").trim(),
    allowedSenderIds,
    stateChangeSenderIds,
    extractionModel,
  };
}


function normalizeSenderIds(value) {
  if (!Array.isArray(value) || value.length === 0) {
    return null;
  }
  const normalized = value.map((item) => String(item).trim()).filter(Boolean);
  return normalized.length > 0 ? normalized : null;
}
