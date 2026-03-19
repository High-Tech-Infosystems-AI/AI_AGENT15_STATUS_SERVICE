pipeline {
  agent {
    kubernetes {
      yaml '''
apiVersion: v1
kind: Pod
metadata:
  labels:
    app: jenkins-agent
spec:
  serviceAccountName: jenkins
  containers:
  - name: kaniko
    image: gcr.io/kaniko-project/executor:debug
    command:
    - cat
    tty: true
    resources:
      requests:
        cpu: 500m
        memory: 1Gi
      limits:
        cpu: 2000m
        memory: 2Gi
    volumeMounts:
    - name: docker-config
      mountPath: /kaniko/.docker
    - name: workspace-volume
      mountPath: /home/jenkins/agent
  - name: kubectl
    image: alpine/k8s:1.30.4
    command:
    - cat
    tty: true
    volumeMounts:
    - name: workspace-volume
      mountPath: /home/jenkins/agent
  volumes:
  - name: docker-config
    secret:
      secretName: supriyo-docker-creds
      optional: true
  - name: workspace-volume
    emptyDir: {}
'''
    }
  }

  options {
    disableConcurrentBuilds()
  }

  environment {
    REGISTRY   = "harbor.htinfosystems.com/hrmis"   // Harbor project path
    IMAGE_NAME = "hrmis-status-service"
  }

  stages {
    stage('Check branch') {
      steps {
        script {
          if (!(env.BRANCH_NAME in ['main', 'staging'])) {
            echo "Branch '${env.BRANCH_NAME}' is not configured for deployment. Stopping pipeline."
            currentBuild.result = 'NOT_BUILT'
            error("Unsupported branch for deployment")
          }
        }
      }
    }

    stage('Checkout') {
      steps {
        checkout scm
      }
    }

    stage('Set Environment') {
      steps {
        script {
          def namespace   = 'hrmis-prod'
          def configMap   = 'hrmis-prod-config'

          if (env.BRANCH_NAME == 'staging') {
            namespace = 'hrmis-stage'
            configMap = 'hrmis-stage-config'
          }

          env.K8S_NAMESPACE   = namespace
          env.CONFIG_MAP_NAME = configMap
          env.IMAGE_TAG       = "${env.BUILD_NUMBER}"
        }
      }
    }

    stage('Prepare Manifests') {
      steps {
        container('kubectl') {
          dir('k8s') {
            sh """
              cp deployment.yaml deployment.rendered.yaml || true

              sed -i "s|__DOCKER_IMAGE__|${REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}|g" deployment.rendered.yaml
              sed -i "s|__CONFIG_MAP_NAME__|${CONFIG_MAP_NAME}|g" deployment.rendered.yaml || true
            """
          }
        }
      }
    }

    stage('Build & Push to Harbor') {
      steps {
        container('kaniko') {
          sh """
            /kaniko/executor \\
              --context=${WORKSPACE} \\
              --dockerfile=${WORKSPACE}/Dockerfile \\
              --destination=${REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG} \\
              --destination=${REGISTRY}/${IMAGE_NAME}:latest \\
              --cache=true \\
              --skip-tls-verify=false
          """
        }
      }
    }

    stage('Deploy to K8s') {
      steps {
        container('kubectl') {
          sh """
            kubectl get namespace ${K8S_NAMESPACE} || kubectl create namespace ${K8S_NAMESPACE}

            kubectl apply -f k8s/deployment.rendered.yaml -n ${K8S_NAMESPACE}
            kubectl rollout status deployment/status-service -n ${K8S_NAMESPACE} --timeout=300s
          """
        }
      }
    }
  }

  post {
    success {
      echo "hrmis-status-service #${BUILD_NUMBER} deployed successfully to ${K8S_NAMESPACE}"
    }
    failure {
      echo "hrmis-status-service build #${BUILD_NUMBER} failed!"
    }
  }
}