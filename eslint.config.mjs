// Catches the bug class that broke Admin > Users in 3.5.1: names used but never defined/imported.
// Run: npm i --no-save eslint@9 globals@15 && npx eslint static/js static/sw.js   (CI runs this on every push)
import globals from "globals";

export default [
  {
    files: ["static/js/**/*.js"],
    languageOptions: { ecmaVersion: 2023, sourceType: "module", globals: { ...globals.browser } },
    rules: { "no-undef": "error", "no-unused-vars": ["warn", { args: "none", ignoreRestSiblings: true, varsIgnorePattern: "^_" }], "no-dupe-keys": "error",
             "no-unreachable": "error", "no-redeclare": "error", "no-import-assign": "error" },
  },
  { files: ["static/sw.js"], languageOptions: { globals: { ...globals.serviceworker } }, rules: { "no-undef": "error" } },
];
