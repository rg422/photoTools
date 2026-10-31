#Requires -Version 5.1
<#
.SYNOPSIS
    Reads a YYYYMMDD date prefix from image file names and stamps that date into
    the file's EXIF "Date taken" plus its filesystem Created / Modified times.

.DESCRIPTION
    Files are NEVER renamed and pixel data is never re-encoded. The EXIF write
    goes through the Windows Property System (the same path File Explorer's
    Details tab uses), so nothing needs to be installed.

    Recognized name prefixes (must be at the very start of the name):
        20020128 Oma-01.JPG
        2002-01-28 Oma-01.JPG
        2002_01_28 Oma-01.JPG
        20020128_134500 Oma-01.JPG   (optional _HHMMSS / -HHMMSS time)

    Anything else is reported as "could not interpret" and left untouched.

.EXAMPLE
    .\Set-PhotoDateFromFilename.ps1 -DryRun
.EXAMPLE
    .\Set-PhotoDateFromFilename.ps1 -Recurse -CsvReport .\photo-dates.csv
#>
[CmdletBinding()]
param(
    # Folder to process. Defaults to the current folder.
    [string]   $Path = '.',

    # Also process sub-folders.
    [switch]   $Recurse,

    # Time of day to use when the file name carries no time. 24h, HH:mm or HH:mm:ss.
    [string]   $Time = '12:00:00',

    # Instead of -Time, keep the time-of-day already on the file's Modified stamp.
    [switch]   $UseFileTime,

    # Only touch Created/Modified timestamps; do not write EXIF.
    [switch]   $SkipMetadata,

    # Report what would happen; change nothing.
    [switch]   $DryRun,

    # Optional path to also write the report as CSV.
    [string]   $CsvReport,

    # File extensions to consider.
    [string[]] $Include = @('.jpg','.jpeg','.jpe','.jfif','.png','.tif','.tiff','.heic','.heif','.gif','.bmp','.webp')
)

$ErrorActionPreference = 'Stop'

# Extensions where an embedded EXIF/metadata date is actually supported.
$metaCapable = @('.jpg','.jpeg','.jpe','.jfif','.tif','.tiff','.heic','.heif')

# ---------------------------------------------------------------- metadata API
$metaAvailable = $true
try {
    if (-not ('PhotoDate.Writer' -as [type])) {
        Add-Type -Language CSharp -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

namespace PhotoDate
{
    [StructLayout(LayoutKind.Sequential, Pack = 4)]
    public struct PropertyKey
    {
        public Guid fmtid;
        public uint pid;
        public PropertyKey(Guid f, uint p) { fmtid = f; pid = p; }
    }

    [StructLayout(LayoutKind.Explicit)]
    public struct PropVariant
    {
        [FieldOffset(0)] public ushort vt;
        [FieldOffset(2)] public ushort r1;
        [FieldOffset(4)] public ushort r2;
        [FieldOffset(6)] public ushort r3;
        [FieldOffset(8)] public long   ft;
    }

    [ComImport, Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IPropertyStore
    {
        void GetCount(out uint cProps);
        void GetAt(uint iProp, out PropertyKey pkey);
        void GetValue(ref PropertyKey key, out PropVariant pv);
        void SetValue(ref PropertyKey key, ref PropVariant pv);
        void Commit();
    }

    public static class Writer
    {
        [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
        private static extern void SHGetPropertyStoreFromParsingName(
            string pszPath, IntPtr pbc, int flags, ref Guid riid,
            [MarshalAs(UnmanagedType.Interface)] out IPropertyStore ppv);

        private static readonly Guid IID_IPropertyStore =
            new Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99");

        // System.Photo.DateTaken -> EXIF 0x9003 DateTimeOriginal
        private static readonly Guid FMTID_Photo =
            new Guid("14B81DA1-0135-4D31-96D9-6CBFC9671A99");

        private const int GPS_READWRITE = 0x2;
        private const ushort VT_FILETIME = 64;

        public static void SetDateTaken(string path, DateTime localTime)
        {
            Guid iid = IID_IPropertyStore;
            IPropertyStore store;
            SHGetPropertyStoreFromParsingName(path, IntPtr.Zero, GPS_READWRITE, ref iid, out store);
            try
            {
                PropertyKey key = new PropertyKey(FMTID_Photo, 36867);
                PropVariant pv = new PropVariant();
                pv.vt = VT_FILETIME;
                pv.ft = localTime.ToFileTime();
                store.SetValue(ref key, ref pv);
                store.Commit();
            }
            finally { if (store != null) Marshal.ReleaseComObject(store); }
        }

        public static string GetDateTaken(string path)
        {
            Guid iid = IID_IPropertyStore;
            IPropertyStore store;
            SHGetPropertyStoreFromParsingName(path, IntPtr.Zero, 0, ref iid, out store);
            try
            {
                PropertyKey key = new PropertyKey(FMTID_Photo, 36867);
                PropVariant pv;
                store.GetValue(ref key, out pv);
                if (pv.vt != VT_FILETIME) return null;
                return DateTime.FromFileTime(pv.ft).ToString("yyyy-MM-dd HH:mm:ss");
            }
            finally { if (store != null) Marshal.ReleaseComObject(store); }
        }
    }
}
'@
    }
} catch {
    $metaAvailable = $false
    Write-Warning "Could not initialize the Windows metadata writer: $($_.Exception.Message)"
    Write-Warning "Continuing with filesystem timestamps only."
}

# ---------------------------------------------------------------- default time
try {
    $defaultTod = [TimeSpan]::Parse($Time)
} catch {
    throw "-Time '$Time' is not a valid time of day. Use HH:mm or HH:mm:ss."
}

# ---------------------------------------------------------------- gather files
$root = (Resolve-Path -LiteralPath $Path).ProviderPath
Write-Host ""
Write-Host "Folder : $root"
if ($DryRun) { Write-Host "Mode   : DRY RUN - nothing will be changed" }
else         { Write-Host "Mode   : applying changes" }
Write-Host ""

$files = Get-ChildItem -LiteralPath $root -File -Recurse:$Recurse |
         Where-Object { $Include -contains $_.Extension.ToLowerInvariant() } |
         Sort-Object FullName

if (-not $files) { Write-Warning "No matching image files found in $root"; return }

# YYYY[-_.]MM[-_.]DD with an optional _HHMMSS / -HHMMSS time, anchored at the start.
$rx = '^(?<y>(?:18|19|20)\d{2})[-_.]?(?<mo>0[1-9]|1[0-2])[-_.]?(?<d>0[1-9]|[12]\d|3[01])(?:[_-](?<h>[01]\d|2[0-3])(?<mi>[0-5]\d)(?<s>[0-5]\d))?(?![0-9])'

$report = New-Object System.Collections.Generic.List[object]
$ok = 0; $failed = 0; $unknown = 0

foreach ($f in $files) {

    $m = [regex]::Match($f.Name, $rx)
    if (-not $m.Success) {
        $unknown++
        $report.Add([pscustomobject]@{
            File = $f.Name; Date = ''; DateTaken = '-'; FileTimes = '-'
            Status = 'UNREADABLE'; Note = 'no YYYYMMDD prefix found'
        })
        continue
    }

    # Build the date. An impossible date (e.g. 20020230) throws and is reported.
    try {
        if ($m.Groups['h'].Success) {
            $stamp = [datetime]::new([int]$m.Groups['y'].Value, [int]$m.Groups['mo'].Value, [int]$m.Groups['d'].Value,
                                     [int]$m.Groups['h'].Value, [int]$m.Groups['mi'].Value, [int]$m.Groups['s'].Value)
        } else {
            if ($UseFileTime) { $tod = $f.LastWriteTime.TimeOfDay } else { $tod = $defaultTod }
            $stamp = ([datetime]::new([int]$m.Groups['y'].Value, [int]$m.Groups['mo'].Value, [int]$m.Groups['d'].Value)).Add($tod)
        }
    } catch {
        $unknown++
        $report.Add([pscustomobject]@{
            File = $f.Name; Date = $m.Value; DateTaken = '-'; FileTimes = '-'
            Status = 'UNREADABLE'; Note = 'prefix is not a real calendar date'
        })
        continue
    }

    $stampText = $stamp.ToString('yyyy-MM-dd HH:mm:ss')
    $notes     = @()
    $hadError  = $false

    # --- EXIF / Date taken -------------------------------------------------
    $ext = $f.Extension.ToLowerInvariant()
    if ($SkipMetadata) {
        $metaResult = 'skipped'
    } elseif (-not $metaAvailable) {
        $metaResult = 'unavailable'
    } elseif ($metaCapable -notcontains $ext) {
        $metaResult = 'n/a'
        $notes += "$ext has no EXIF date field"
    } elseif ($DryRun) {
        $metaResult = 'would set'
    } else {
        try {
            $wasReadOnly = $f.IsReadOnly
            if ($wasReadOnly) { $f.IsReadOnly = $false }
            [PhotoDate.Writer]::SetDateTaken($f.FullName, $stamp)
            if ($wasReadOnly) { $f.IsReadOnly = $true }
            $metaResult = 'set'
        } catch {
            $metaResult = 'FAILED'
            $hadError = $true
            $notes += "EXIF: $($_.Exception.Message -replace '\s+',' ')"
        }
    }

    # --- Filesystem timestamps --------------------------------------------
    if ($DryRun) {
        $fsResult = 'would set'
    } else {
        try {
            $wasReadOnly = $f.IsReadOnly
            if ($wasReadOnly) { $f.IsReadOnly = $false }
            $f.CreationTime  = $stamp
            $f.LastWriteTime = $stamp
            if ($wasReadOnly) { $f.IsReadOnly = $true }
            $fsResult = 'set'
        } catch {
            $fsResult = 'FAILED'
            $hadError = $true
            $notes += "timestamps: $($_.Exception.Message -replace '\s+',' ')"
        }
    }

    if ($hadError) { $failed++ } else { $ok++ }

    $report.Add([pscustomobject]@{
        File      = $f.Name
        Date      = $stampText
        DateTaken = $metaResult
        FileTimes = $fsResult
        Status    = $(if ($hadError) { 'ERROR' } else { 'OK' })
        Note      = ($notes -join '; ')
    })
}

# ---------------------------------------------------------------------- report
Write-Host "==================== REPORT ====================" -ForegroundColor Cyan
$report | Format-Table File, Date, DateTaken, FileTimes, Status -AutoSize | Out-String -Width 400 | Write-Host

$problems = $report | Where-Object { $_.Status -ne 'OK' }
if ($problems) {
    Write-Host "--------- Files needing attention ---------" -ForegroundColor Yellow
    foreach ($p in $problems) {
        $reason = $(if ($p.Note) { $p.Note } else { 'unknown' })
        Write-Host ("  [{0}] {1}  ->  {2}" -f $p.Status, $p.File, $reason) -ForegroundColor Yellow
    }
    Write-Host ""
}

Write-Host ("Total images examined  : {0}" -f $report.Count)
Write-Host ("Dated successfully     : {0}" -f $ok)      -ForegroundColor Green
Write-Host ("Errors while writing   : {0}" -f $failed)  -ForegroundColor $(if ($failed)  { 'Red' }    else { 'Gray' })
Write-Host ("Date not interpretable : {0}" -f $unknown) -ForegroundColor $(if ($unknown) { 'Yellow' } else { 'Gray' })
if ($DryRun) { Write-Host "`n(dry run - no files were modified)" -ForegroundColor Cyan }

if ($CsvReport) {
    $report | Export-Csv -LiteralPath $CsvReport -NoTypeInformation -Encoding UTF8
    Write-Host "`nCSV report written to $((Resolve-Path -LiteralPath $CsvReport).ProviderPath)"
}
