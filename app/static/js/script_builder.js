(() => {
  const platform = document.getElementById("scriptPlatform");
  const language = document.getElementById("scriptLanguage");
  const source = document.getElementById("scriptSourceInput");
  const label = document.getElementById("sourceLanguageLabel");
  const help = document.getElementById("languageCompatibility");
  if (!platform || !language || !source) return;

  const allowed = {
    "Windows": ["PowerShell", "Batch"],
    "Linux": ["Bash", "PowerShell"],
    "macOS": ["Zsh", "Bash", "PowerShell"],
    "Cross-platform": ["PowerShell"]
  };
  const templates = {
    "PowerShell": '[CmdletBinding()]\nparam()\n\n$ErrorActionPreference = "Stop"\n',
    "Bash": '#!/usr/bin/env bash\nset -euo pipefail\n\n',
    "Zsh": '#!/usr/bin/env zsh\nset -euo pipefail\n\n',
    "Batch": '@echo off\nsetlocal EnableExtensions\n\n'
  };
  const knownTemplates = new Set(Object.values(templates).map(value => value.trim()));

  const update = (replaceTemplate) => {
    const valid = allowed[platform.value] || [];
    [...language.options].forEach(option => { option.disabled = !valid.includes(option.value); });
    if (!valid.includes(language.value)) language.value = valid[0];
    if (replaceTemplate && (!source.value.trim() || knownTemplates.has(source.value.trim()))) {
      source.value = templates[language.value];
    }
    if (label) label.textContent = language.value;
    if (help) help.textContent = `${platform.value} supports ${valid.join(" and ")}.`;
  };
  platform.addEventListener("change", () => update(true));
  language.addEventListener("change", () => update(true));
  update(false);
})();
