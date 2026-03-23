#!/bin/bash
# Validates dashboard.html JavaScript before deploy.
# Catches: syntax errors, duplicate declarations, hoisting bugs.
# Run: bash tests/test_dashboard_syntax.sh

set -e
DASHBOARD="docs/dashboard.html"

echo "=== Dashboard JS Syntax Check ==="

node -e "
const fs = require('fs');
const html = fs.readFileSync('$DASHBOARD', 'utf8');
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
if (!scriptMatch) { console.error('FAIL: No script block found'); process.exit(1); }
try {
  new Function(scriptMatch[1]);
  console.log('PASS: JS syntax valid — no SyntaxError');
} catch(e) {
  console.error('FAIL:', e.message);
  // Show which line
  const lineMatch = e.message.match(/line (\d+)/i);
  if (!lineMatch) {
    const lines = scriptMatch[1].split('\n');
    // Try to find the problematic identifier
    const identMatch = e.message.match(/Identifier '(\w+)'/);
    if (identMatch) {
      const name = identMatch[1];
      const occurrences = [];
      lines.forEach((l, i) => {
        if (l.match(new RegExp('(?:const|let)\\\\s+' + name + '\\\\s*='))) {
          occurrences.push(i + 1);
        }
      });
      if (occurrences.length > 1) {
        console.error('  Duplicate const/let \"' + name + '\" at script lines: ' + occurrences.join(', '));
      }
    }
  }
  process.exit(1);
}
"

echo "=== Dashboard JS OK ==="
