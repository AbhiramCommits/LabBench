pipeline {
    agent any

    triggers {
        cron('H 2 * * *')
    }

    options {
        timestamps()
        buildDiscarder(logRotator(numToKeepStr: '30', artifactNumToKeepStr: '10'))
        timeout(time: 90, unit: 'MINUTES')
    }

    environment {
        LABBENCH = 'labbench'
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Lint') {
            steps {
                sh 'make install'
                sh 'make lint'
            }
        }

        stage('Unit Tests') {
            steps {
                sh 'make test'
            }
        }

        stage('Lab Up') {
            steps {
                sh 'make lab-up'
            }
        }

        stage('Provision') {
            steps {
                sh '.venv/bin/labbench provision'
            }
        }

        stage('Attach') {
            steps {
                sh '.venv/bin/labbench attach'
            }
        }

        stage('Validate') {
            steps {
                // Validate exits non-zero on failures; keep the pipeline going
                // so the Report stage can publish triage and JUnit XML.
                catchError(buildResult: 'SUCCESS', stageResult: 'FAILURE') {
                    sh '.venv/bin/labbench validate'
                }
            }
        }

        stage('Collect') {
            steps {
                sh '.venv/bin/labbench collect'
            }
        }

        stage('Report') {
            steps {
                // Report exits non-zero when any validation case failed, which
                // fails the build after artifacts are produced.
                sh '.venv/bin/labbench report'
            }
        }
    }

    post {
        always {
            junit 'artifacts/junit.xml'
            archiveArtifacts artifacts: 'artifacts/**', allowEmptyArchive: true, fingerprint: true
            script {
                if (fileExists('artifacts/report.md')) {
                    def lines = readFile('artifacts/report.md').readLines()
                    def triage = lines.findAll { it.startsWith('|') || it.contains('TRIAGE') || it.contains('passed=') }
                    echo "==================== LABBENCH TRIAGE SUMMARY ===================="
                    triage.take(40).each { echo it }
                    echo "================================================================="
                } else {
                    echo "No report.md produced."
                }
            }
            // Never leave a lab running.
            sh 'make lab-down || true'
        }
        failure {
            echo 'LabBench pipeline failed - see artifacts/report.html and the TRIAGE section.'
        }
    }
}
