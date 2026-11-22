$ErrorActionPreference = 'Stop'
$secretPath = Join-Path $PSScriptRoot '.secrets'
[System.IO.Directory]::CreateDirectory($secretPath) | Out-Null
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$acl = New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
$acl.SetOwner($identity.User)
$inheritance = [System.Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
foreach ($sid in @($identity.User, [System.Security.Principal.SecurityIdentifier]'S-1-5-18')) {
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', $inheritance, 'None', 'Allow')
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $secretPath -AclObject $acl
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
try {
    foreach ($name in @('account-key')) {
        $target = Join-Path $secretPath $name
        if (-not (Test-Path -LiteralPath $target)) {
            $bytes = New-Object byte[] 32
            $rng.GetBytes($bytes)
            $value = [Convert]::ToBase64String($bytes).Replace('+','-').Replace('/','_')
            [System.IO.File]::WriteAllText($target, $value + [Environment]::NewLine)
        }
    }
} finally { $rng.Dispose() }
Write-Output 'Encryption key initialized with private permissions. Existing keys were preserved. Create the dashboard password in the WebUI.'
