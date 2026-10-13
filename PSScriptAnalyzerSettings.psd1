@{
    # LabBench PowerShell analysis settings.
    Severity            = @('Error', 'Warning')
    ExcludeRules        = @(
        'PSAvoidUsingWriteHost',
        'PSUseShouldProcessForStateChangingFunctions',
        'PSUseApprovedVerbs',
        'PSAvoidUsingPositionalParameters'
    )
}
