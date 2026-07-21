[CmdletBinding()]
param(
    # Provider names are validated by dispatch.py against your providers.toml
    # (it prints a helpful "unknown provider" error listing the configured ones),
    # so this wrapper deliberately accepts any string.
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Provider,

    [Parameter(ValueFromPipeline = $true)]
    [AllowEmptyString()]
    [string]$Prompt,

    [string]$Label = '',
    [string]$Model = '',

    [ValidateRange(1, 2147483647)]
    [int]$Timeout = 300,

    [switch]$RetryNoResult,

    [ValidateSet('review', 'text')]
    [string]$ClaudeProfile = 'review',

    [ValidateSet('', 'low', 'medium', 'high', 'xhigh', 'max')]
    [string]$Effort = '',

    [double]$MaxBudgetUsd = 0,

    [string]$DispatchPath = (Join-Path $PSScriptRoot 'dispatch.py')
)

begin {
    $promptParts = [System.Collections.Generic.List[string]]::new()
}

process {
    $promptParts.Add($Prompt)
}

end {
    $utf8NoBom = [System.Text.UTF8Encoding]::new($false)
    $previousOutputEncoding = $OutputEncoding
    $hadTransportMarker = Test-Path Env:AI_ORCHESTRA_UTF8_STDIN_VERIFIED
    $previousTransportMarker = $env:AI_ORCHESTRA_UTF8_STDIN_VERIFIED
    $OutputEncoding = $utf8NoBom
    $env:AI_ORCHESTRA_UTF8_STDIN_VERIFIED = '1'

    try {
        $python = (Get-Command python -ErrorAction Stop).Source
        $arguments = [System.Collections.Generic.List[string]]::new()
        $arguments.Add($DispatchPath)
        $arguments.Add($Provider)
        $arguments.Add('--timeout')
        $arguments.Add($Timeout.ToString([System.Globalization.CultureInfo]::InvariantCulture))

        if ($Label) {
            $arguments.Add('--label')
            $arguments.Add($Label)
        }
        if ($Model) {
            $arguments.Add('--model')
            $arguments.Add($Model)
        }
        if ($RetryNoResult) {
            $arguments.Add('--retry-no-result')
        }
        if ($ClaudeProfile -ne 'review') {
            $arguments.Add('--claude-profile')
            $arguments.Add($ClaudeProfile)
        }
        if ($Effort) {
            $arguments.Add('--effort')
            $arguments.Add($Effort)
        }
        if ($PSBoundParameters.ContainsKey('MaxBudgetUsd')) {
            if (
                [double]::IsNaN($MaxBudgetUsd) -or
                [double]::IsInfinity($MaxBudgetUsd) -or
                $MaxBudgetUsd -le 0
            ) {
                throw 'MaxBudgetUsd must be finite and greater than zero.'
            }
            $arguments.Add('--max-budget-usd')
            $arguments.Add($MaxBudgetUsd.ToString([System.Globalization.CultureInfo]::InvariantCulture))
        }

        $joinedPrompt = [string]::Join([System.Environment]::NewLine, $promptParts)
        $joinedPrompt | & $python @arguments
        exit $LASTEXITCODE
    }
    finally {
        $OutputEncoding = $previousOutputEncoding
        if ($hadTransportMarker) {
            $env:AI_ORCHESTRA_UTF8_STDIN_VERIFIED = $previousTransportMarker
        }
        else {
            Remove-Item Env:AI_ORCHESTRA_UTF8_STDIN_VERIFIED -ErrorAction SilentlyContinue
        }
    }
}
