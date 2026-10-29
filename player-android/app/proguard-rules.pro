# RD Player — regras R8 (release).
# O código do app é mantido integralmente (componentes instanciados pelo sistema: Activity, Receivers,
# Worker do WorkManager, DeviceAdminReceiver, entidades/DAOs do Room). R8 reduz apenas as bibliotecas.
-keep class br.com.rdmidiaindoor.player.** { *; }
-keepattributes SourceFile,LineNumberTable,Signature,*Annotation*
# OkHttp: avisos de plataformas opcionais (Conscrypt/BouncyCastle/OpenJSSE) não usadas.
-dontwarn org.conscrypt.**
-dontwarn org.bouncycastle.**
-dontwarn org.openjsse.**
