export const INTERACTIVE_NAMESPACE = "wife-roster";


export function registerWifeRosterInteractiveHandler(api, controller) {
  api.registerInteractiveHandler({
    channel: "telegram",
    namespace: INTERACTIVE_NAMESPACE,
    handler: async (ctx) => {
      // Telegram core acknowledges the callback query before it invokes a
      // registered plugin handler. This handler therefore performs only the
      // deterministic state transition and message update.
      try {
        return await controller.handleInteractive(ctx);
      } catch {
        api.logger.warn?.("wife-roster interactive request failed safely");
        try {
          await ctx.respond.reply({
            text: "wife-roster could not complete this request safely.",
          });
        } catch {
          // Callback acknowledgement is already complete. Contain a reply
          // failure and never allow the payload to reach an agent/model path.
        }
        return { handled: true };
      }
    },
  });
}
