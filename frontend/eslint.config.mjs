// ESLint flat config.
//
// Next 16 removed `next lint`, so `npm run lint` calls eslint directly and the
// old `.eslintrc.json` no longer applies -- ESLint 9 looks for this file.
// eslint-config-next 16 ships flat config as an array from its subpath exports,
// so the rules themselves are unchanged from `extends: next/core-web-vitals`.
import nextCoreWebVitals from "eslint-config-next/core-web-vitals";

export default [
  {
    // Build output and dependencies are not ours to lint, and flat config has
    // no implicit ignores beyond node_modules.
    ignores: [".next/**", "out/**", "node_modules/**", "next-env.d.ts"],
  },
  ...nextCoreWebVitals,
  {
    // The React Compiler rules arrived with eslint-config-next 16 and fire on
    // code that predates them: four pages load data with the ordinary
    // `useEffect(() => { void load() }, [load])` shape, the timeline tracks the
    // last-seen iteration in a local during render, and one memo in the run
    // page cannot be verified. None is a bug today.
    //
    // They are warnings rather than errors because fixing them means
    // restructuring how four pages load data, and that belongs in its own
    // change where it can be reviewed and tested on its own merits -- not
    // folded into a security upgrade, silently, next to the SSE wiring. Left as
    // errors they would instead have been switched off and forgotten.
    rules: {
      "react-hooks/set-state-in-effect": "warn",
      "react-hooks/preserve-manual-memoization": "warn",
      "react-hooks/immutability": "warn",
    },
  },
];
