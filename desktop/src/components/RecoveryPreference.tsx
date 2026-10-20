import type { Settings } from "../types";

interface Props {
  settings: Settings;
  onChange: (settings: Settings) => void;
  disabled?: boolean;
}

/** Local recovery policy, separate from Google Drive revision retention. */
export function RecoveryPreference({
  settings,
  onChange,
  disabled = false,
}: Props) {
  return (
    <div className="setting-row">
      <div>
        <label htmlFor="keep-local-backups">Keep recovery copies</label>
        <p id="keep-local-backups-description">
          Save local files in .permafrost/backups before replacing or deleting
          them during sync. Turning this off permanently removes those old local
          copies. Drive deletions still go to Trash.
        </p>
        <p>Applies to future syncs. Existing recovery copies are kept.</p>
      </div>
      <input
        id="keep-local-backups"
        type="checkbox"
        role="switch"
        aria-describedby="keep-local-backups-description"
        checked={settings.keepLocalBackups}
        disabled={disabled}
        onChange={(event) =>
          onChange({ ...settings, keepLocalBackups: event.target.checked })
        }
      />
    </div>
  );
}
