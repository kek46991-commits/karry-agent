import Combine
import CoreLocation
import Foundation

/// Opt-in significant-change tracking. iOS keeps the service alive after the app closes
/// only while the user has granted Always access and the visible toggle remains on.
@MainActor
final class BackgroundLocationService: NSObject, ObservableObject, CLLocationManagerDelegate {
    @Published private(set) var enabled: Bool
    @Published private(set) var status = "オフ"
    @Published private(set) var lastSentAt: Date?

    private let manager = CLLocationManager()
    private let settings: OperatorSettings
    private let tokenStore: TokenStoring
    private var sending = false

    init(settings: OperatorSettings, tokenStore: TokenStoring) {
        self.settings = settings
        self.tokenStore = tokenStore
        enabled = settings.locationSharing
        super.init()
        manager.delegate = self
        manager.activityType = .other
        manager.desiredAccuracy = kCLLocationAccuracyHundredMeters
        manager.distanceFilter = 200
        manager.pausesLocationUpdatesAutomatically = true
        manager.allowsBackgroundLocationUpdates = true
        manager.showsBackgroundLocationIndicator = true
        refresh()
    }

    func setEnabled(_ value: Bool) {
        enabled = value
        settings.locationSharing = value
        if value {
            manager.requestAlwaysAuthorization()
        } else {
            manager.stopMonitoringSignificantLocationChanges()
        }
        refresh()
    }

    func refresh() {
        guard enabled else {
            manager.stopMonitoringSignificantLocationChanges()
            status = "オフ"
            return
        }
        guard tokenStore.readToken() != nil else {
            status = "端末登録が必要です"
            return
        }
        switch manager.authorizationStatus {
        case .authorizedAlways:
            manager.startMonitoringSignificantLocationChanges()
            status = lastSentAt == nil ? "記録中（移動待ち）" : "記録中"
        case .authorizedWhenInUse:
            status = "設定で位置情報を「常に」にしてください"
        case .notDetermined:
            status = "位置情報の許可待ち"
        case .denied, .restricted:
            status = "位置情報が許可されていません"
        @unknown default:
            status = "位置情報を確認できません"
        }
    }

    nonisolated func locationManagerDidChangeAuthorization(_ manager: CLLocationManager) {
        Task { @MainActor in self.refresh() }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didUpdateLocations locations: [CLLocation]) {
        guard let location = locations.last, location.horizontalAccuracy >= 0 else { return }
        Task { @MainActor in await self.send(location) }
    }

    nonisolated func locationManager(_ manager: CLLocationManager, didFailWithError error: Error) {
        Task { @MainActor in
            if self.enabled { self.status = "位置情報を取得できません" }
        }
    }

    private func send(_ location: CLLocation) async {
        guard enabled, !sending, let token = tokenStore.readToken() else { return }
        sending = true
        defer { sending = false }
        do {
            guard let url = URL(string: settings.baseURL) else { throw BackendError.invalidBaseURL }
            let backend = try BackendClient(baseURL: url)
            try await backend.reportLocation(
                deviceID: settings.deviceID,
                deviceToken: token,
                latitude: location.coordinate.latitude,
                longitude: location.coordinate.longitude,
                accuracyM: location.horizontalAccuracy,
                collectedAt: location.timestamp
            )
            lastSentAt = Date()
            status = "記録中"
        } catch BackendError.unauthorized {
            status = "再登録が必要です"
        } catch {
            status = "送信待ち（次の移動時に再試行）"
        }
    }
}
