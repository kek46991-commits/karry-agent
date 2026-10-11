# karry-agent 本番デプロイ設計

## 方針

- **実行単位**: 現在の `karry-agent` は対話型 CLI のため、Helm の `Deployment` をワーカー実行単位として定義します。HTTP API / キューコンシューマーを追加する Phase では `command` と `args` を差し替え、Service と readiness probe を追加してください。
- **クラウド**: GKE（Google Cloud）または EKS（AWS）で利用できるクラウド非依存の Kubernetes リソースにしています。
- **秘密情報**: `GEMINI_API_KEY` は Git にコミットせず、既存 Kubernetes Secret、External Secrets Operator、GCP Secret Manager / AWS Secrets Manager などから同期します。
- **永続データ**: `progress.md` は現在 `emptyDir` に保存されます。本番で監査ログを保持する場合は Cloud Logging / CloudWatch、または PVC・外部 DB に切り替えてください。

## 構成

```text
Container Registry (GHCR/GAR/ECR)
          |
          v
GKE / EKS cluster
  └─ karry-agent Deployment
      ├─ ConfigMap: model / workspace
      ├─ Secret: GEMINI_API_KEY（外部 Secret 推奨）
      ├─ ServiceAccount（最小権限）
      ├─ PDB / NetworkPolicy
      └─ optional HPA
```

Chart は `deploy/helm/karry-agent` にあります。

## 事前準備

1. `Dockerfile` をビルドし、GHCR / Artifact Registry / ECR へ push します。
2. クラスタの namespace を作成します。
3. Secret をクラスタ内で作成します（値はコマンド履歴へ残さない方法を推奨）。

```bash
kubectl create namespace karry-agent
kubectl -n karry-agent create secret generic karry-agent-secrets \
  --from-literal=GEMINI_API_KEY="$GEMINI_API_KEY"
```

より安全な本番構成では、Secret Manager と External Secrets Operator を使い、上記 Secret を手動作成しません。

## Helm デプロイ

```bash
helm upgrade --install karry-agent ./deploy/helm/karry-agent \
  --namespace karry-agent \
  --set image.repository=REGISTRY/karry-agent \
  --set image.tag=IMAGE_TAG \
  --set existingSecret=karry-agent-secrets \
  --wait --timeout 5m
```

確認:

```bash
kubectl -n karry-agent get deploy,pods,pdb,networkpolicy
kubectl -n karry-agent logs -l app.kubernetes.io/instance=karry-agent --tail=100
helm status karry-agent -n karry-agent
```

## GKE / EKS 差分

- **GKE**: Workload Identity を使う場合は `serviceAccount.annotations` に GCP IAM Service Account を設定し、`existingSecret` は Secret Manager 連携へ置き換えます。イメージは Artifact Registry を推奨します。
- **EKS**: IRSA を使う場合は `serviceAccount.annotations` に IAM Role ARN を設定し、External Secrets から AWS Secrets Manager を参照します。イメージは ECR を推奨します。
- どちらも Kubernetes の ServiceAccount にクラウド管理者権限を与えず、必要な Secret 読み取りだけを許可してください。

## 本番チェックリスト

- [ ] Secret が Git、Dockerfile、values ファイルに含まれていない
- [ ] レジストリの immutable tag / digest を使用している
- [ ] `existingSecret` が設定されている
- [ ] CPU / メモリ requests と limits を実測値へ調整した
- [ ] NetworkPolicy と egress の組織要件を確認した
- [ ] ログ・アラート・Secret ローテーションを設定した
- [ ] CLI のままでは対話入力がないため、実運用では API / キューアダプターを実装した
- [ ] `helm lint` と staging cluster への dry-run を実行した

## ローカル検証

```bash
helm lint deploy/helm/karry-agent
helm template karry-agent deploy/helm/karry-agent \
  --set existingSecret=karry-agent-secrets \
  --set image.repository=example/karry-agent \
  --set image.tag=test
kubectl apply --dry-run=client -f rendered.yaml
```

この Sandbox には Docker / Helm / kubectl がないため、上記コマンドによる実クラスタ検証は未実行です。
