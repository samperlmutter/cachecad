import { signDevelopment } from "./development-signing.mjs";
if (!process.argv[2]) throw new Error("Expected the Cargo executable path.");
signDevelopment(process.argv[2]);
