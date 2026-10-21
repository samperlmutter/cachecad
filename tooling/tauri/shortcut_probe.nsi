Unicode true
SilentInstall silent
RequestExecutionLevel user
OutFile "${PROBE_EXE}"
!include LogicLib.nsh
!include Win\COM.nsh
!include Win\WinDef.nsh

Section
  FileOpen $R9 "${REPORT}" w
  !insertmacro ComHlpr_CreateInProcInstance ${CLSID_ShellLink} ${IID_IShellLink} r0 ".r4"
  FileWrite $R9 "create=$0 hresult=$4$\r$\n"
  ${If} $0 P<> 0
    ${IUnknown::QueryInterface} $0 '("${IID_IPersistFile}", .r1).r4'
    FileWrite $R9 "query=$1 hresult=$4$\r$\n"
    ${If} $1 P<> 0
      ${IPersistFile::Load} $1 '("${SHORTCUT}", 0).r4'
      FileWrite $R9 "load hresult=$4$\r$\n"
      System::Alloc MAX_PATH
      Pop $2
      FileWrite $R9 "allocation=$2$\r$\n"
      ${IShellLink::GetPath} $0 '(.r2, 260, 0, 4).r4'
      FileWrite $R9 "path=$2 hresult=$4 expected=${TARGET}$\r$\n"
      StrCpy $3 0
      ${If} $2 == "${TARGET}"
        StrCpy $3 1
      ${EndIf}
      FileWrite $R9 "matches=$3$\r$\n"
      System::Free $2
      ${IUnknown::Release} $1 ""
      FileWrite $R9 "after release matches=$3$\r$\n"
    ${EndIf}
    ${IUnknown::Release} $0 ""
  ${EndIf}
  FileClose $R9
SectionEnd
