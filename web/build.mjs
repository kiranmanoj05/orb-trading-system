import { build } from "esbuild";
import { mkdir, copyFile, readFile, writeFile } from "node:fs/promises";
await mkdir("build", { recursive: true });
await build({
  entryPoints: ["src/app.tsx"],
  bundle: true,
  minify: true,
  format: "esm",
  outdir: "build",
  entryNames: "app",
  define: { "process.env.NODE_ENV": '"production"' },
  legalComments: "eof",
});
await copyFile("index.html", "build/index.html");
await writeFile(
  "build/THIRD_PARTY_NOTICES.txt",
  "Bundled runtime dependencies\n\n" +
    (
      await Promise.all(
        ["react", "react-dom"].map(
          async (name) =>
            name +
            "\n" +
            (await readFile(`node_modules/${name}/LICENSE`, "utf8")),
        ),
      )
    ).join("\n\n"),
);
