export const CONFIG_WARNING_BANNER_CLASS = "scan-message scan-message-config";

export function isScannerConfigurationWarning(text: string): boolean {
  return text.trim().toLowerCase().startsWith("scanner_configuration:");
}

export function configWarningsImplyOutage(warnings: string[]): boolean {
  return warnings.some((item) => {
    if (/\bnot a provider outage\b/i.test(item)) return false;
    return /\b(provider outage|provider failure|venue outage|fake provider)\b/i.test(item);
  });
}
