pipeline {
  agent any

  options {
    disableConcurrentBuilds()
  }

  environment {
    // Fixed to QA; defaults set to avoid unset-variable errors
    APP_ENV    = 'qa'
    APP_PORT   = '8515'
    IMAGE_NAME = 'ats-agent15-status-service:qa'
    CONTAINER  = 'ats-agent15-status-qa'
    LOG_PATH   = '/home/supriyo/ai_agents_qa/LOGS'
    NETWORK    = 'ats-qa-network'
  }

  stages {
    stage('Guard Branch') {
      when { expression { return env.BRANCH_NAME != null } }
      steps {
        script {
          if (env.BRANCH_NAME != 'qa') {
            echo "Skipping build: only qa branch is allowed (current: ${env.BRANCH_NAME})"
            currentBuild.result = 'NOT_BUILT'
            return
          }
        }
      }
    }

    stage('Init') {
      when { branch 'qa' }
      steps {
        script {
          // For now we deploy only QA. To re-enable branch-based mapping:
          // def branch = env.BRANCH_NAME ?: 'unknown'
          // def envMap = [
          //   'main'    : 'prod',
          //   'master'  : 'prod',
          //   'qa'      : 'qa',
          //   'release' : 'qa',
          //   'develop' : 'dev',
          //   'dev'     : 'dev'
          // ]
          // env.APP_ENV = envMap.get(branch, 'qa')
          env.APP_ENV = 'qa'    

          // Map env → port; adjust if you prefer different ports
          def portMap = [dev: '8415', qa: '8515', prod: '8615']
          def networkMap = [dev: 'ats-dev-network', qa: 'ats-qa-network', prod: 'ats-prod-network']
          env.APP_PORT   = portMap[env.APP_ENV]
          env.IMAGE_NAME = "ats-agent15-status-service:${env.APP_ENV}"
          env.CONTAINER  = "ats-agent15-status-${env.APP_ENV}"
          // Log path fixed per request; adjust map if enabling other envs
          env.LOG_PATH   = "/home/supriyo/ai_agents_qa/LOGS"
          env.NETWORK    = networkMap[env.APP_ENV]
        }
      }
    }

    stage('Checkout') {
      when { branch 'qa' }
      steps {
        checkout scm
      }
    }

    stage('Build Image') {
      when { branch 'qa' }
      steps {
        sh """
          docker build -t ${IMAGE_NAME} .
        """
      }
    }

    stage('Stop Old Container') {
      when { branch 'qa' }
      steps {
        sh """
          docker stop ${CONTAINER} 2>/dev/null || true
          docker rm -f ${CONTAINER} 2>/dev/null || true
        """
      }
    }

    stage('Run Container') {
      when { branch 'qa' }
      steps {
        script {
          // Expect per-env Jenkins credentials:
          // dbCreds-<env>      : usernamePassword (DB user/pass)
          // dbHost-<env>       : secret text (DB_HOST)
          // dbPort-<env>       : secret text (DB_PORT)
          // dbName-<env>       : secret text (DB_NAME)
          // Credentials IDs are used as-is (no env suffix appended)
          def cred = { base -> base }

          withCredentials([
            string(credentialsId: cred('AI-DB-QA-USER'), variable: 'DB_USER'),
            string(credentialsId: cred('AI-DB-QA-PASS'), variable: 'DB_PASSWORD'),
            string(credentialsId: cred('AI-DB-HOST'), variable: 'DB_HOST'),
            string(credentialsId: cred('AI-DB-PORT'), variable: 'DB_PORT'),
            string(credentialsId: cred('AI-DB-NAME-QA'), variable: 'DB_NAME'),
            string(credentialsId: cred('REDIS-HOST'), variable: 'REDIS_HOST'),
            string(credentialsId: cred('REDIS-PORT'), variable: 'REDIS_PORT'),
          ]) {
            sh """
              docker run -d --name ${CONTAINER} --restart unless-stopped \\
                --add-host=host.docker.internal:host-gateway \\
                --network ${NETWORK} \\
                -p ${APP_PORT}:${APP_PORT} \\
                -e APP_ENV=${env.APP_ENV} \\
                -e APP_PORT=${APP_PORT} \\
                -e DB_HOST=$DB_HOST \\
                -e DB_PORT=$DB_PORT \\
                -e DB_NAME=$DB_NAME \\
                -e DB_USER=$DB_USER \\
                -e DB_PASSWORD=$DB_PASSWORD \\
                -e REDIS_HOST=$REDIS_HOST \\
                -e REDIS_PORT=$REDIS_PORT \\
                -e REDIS_PASSWORD='' \\
                -e REDIS_DB=0 \\
                -e FILE_HANDLER_LOG=${LOG_PATH} \\
                -v ${LOG_PATH}:${LOG_PATH} \\
                ${IMAGE_NAME}
            """
          }
        }
      }
    }
  }

  post {
    always {
      sh 'command -v docker >/dev/null 2>&1 && docker ps -a --filter "name=ats-agent15-status" || echo "docker not available on agent"'
    }
    failure {
      echo "Deployment failed for ${env.APP_ENV}"
    }
  }
}
