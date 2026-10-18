CacheCAD Offline Development 0.1.0

This is an unsigned personal/noncommercial development tryout of a reconstructed
application. It is not the original vendor installer or a production release.

The actual reconstructed Qt window and controller run with authored placeholder
artwork. Every launch uses disposable temporary data, removed on normal exit.
Sign-in, transfers, updates, external links and real-project selection are disabled.
No credentials, vendor artwork, real CAD projects or account configuration are
included. The installer does not start the application automatically.

The console is intentional: it exposes diagnostic output and test results.
Offline tripwires are defense in depth for reviewed application code, not an OS
sandbox against malicious native code. Do not supply real projects or credentials.
Source hashes detect unexpected source changes; they are not a signed trust chain.

The program installs for the current user and supplies Start Menu shortcuts plus
an uninstaller. It adds no service, startup task, file association or network rule.

CPython's license notice is under _internal/third-party-notices/Python. Original
PySide6, Qt, shiboken6, Send2Trash, tzlocal, tzdata and PyInstaller notices are
preserved in _internal distribution metadata/license directories. Qt DLLs remain
separate in the onedir bundle. The app source is under _internal/app-source.
This notice does not grant rights to upstream recovered code or vendor branding;
check relevant rights and third-party license obligations before redistribution.
